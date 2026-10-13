# """
# smt_schedule_service.py
# =======================
# Standalone SMT finite-capacity scheduling service, in ONE file:

#     part 1  scheduling engine   (was smt_scheduler.py  - pure python, no database)
#     part 2  database layer      (was smt_schedule_api.py - reads the MES tables, builds the plan)
#     part 3  FastAPI app         (runs on its own, port 2000 by default)

# Run
#     python smt_schedule_service.py
#     or:  uvicorn smt_schedule_service:app --host 0.0.0.0 --port 2000

# Needs Database.py (session, engine) and Database_models.py in the same folder - the same two files
# Data_Server.py uses. Python packages: fastapi, uvicorn, sqlalchemy, pydantic
# (+ python-dotenv if you use a .env file, + python-jose if CHECK_JWT_VALIDATION is "True").

# Environment (optional - read from .env when python-dotenv is installed)
#     SCHEDULE_SERVICE_PORT       port, default 2000
#     CHECK_JWT_VALIDATION        "True" -> every endpoint needs the x-access-token header (same as Data_Server)
#     JWT_SECRET, ALGORITHM       used when CHECK_JWT_VALIDATION is "True"
#     OPERATOR_LOG_SHIFT_MINUTES  minutes added to operator_log start/end times when they are read.
#                                 /operator/updatestatus writes them with datetime.utcnow(), so the default
#                                 "auto" adds this server's UTC offset (330 on an IST server). Set 0 if your
#                                 operator_log times are already local time.
#     STALE_RUNNING_HOURS         a serial still "Started" with no action for longer than this is not treated as
#                                 occupying its operator / machine now (forgotten scans). Default 12.
#     HISTORY_MERGE_GAP_MINUTES   consecutive serials of the same PO + stage by the same operator are drawn as one
#                                 bar when the gap between them is at most this. Default 30.

# Changes compared with the two original files
#     1. Running / finished work is reserved on its resource BEFORE new work is planned, so new work is never
#        placed on top of it. If the machine of running work is only a guess, it is moved to a free parallel
#        machine of the same stage instead of piling up on one machine (cause of several serials / POs
#        showing on the same machine at the same time in the Gantt).
#     2. The resource actually used is taken from existing data where possible, in this order:
#        operator_log operator (MANUAL stages) -> only one resource at the stage -> a machine field inside
#        operator_log.log_Data -> spc_data_log. Otherwise the baseline plan's machine is used (old behaviour).
#        Each operation in the response has "resource_source" telling which one was used.
#     3. Operator MRL expiry stored as MMYY (e.g. "0127" or "127.0") is understood. It was silently ignored.
#     4. The same machine mapped to several stages (one equipment_details row per stage) is ONE resource and
#        can no longer be double-booked across stages. Set SHARE_MACHINE_ACROSS_STAGES = False for old behaviour.
#     5. One operator / machine works on one serial at a time:
#        - work already done is drawn from the real serial times in operator_log (one bar per run of consecutive
#          serials, "entry_type": "HISTORY"), not as one long bar from the first to the last serial of the PO.
#          When the logs of one operator overlap (several serials scanned "Started" together), each serial is
#          credited only the time between the operator's previous completion and its own completion.
#        - the remaining work of started POs is queued one after another on the operator / machine
#          ("entry_type": "REMAINING") instead of all running in parallel from "now".
# """
# from __future__ import annotations

# import math
# import os
# import re
# from collections import Counter, defaultdict
# from dataclasses import dataclass, field
# from datetime import date, datetime, time, timedelta
# from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
# from fastapi.middleware.cors import CORSMiddleware
# from pydantic import BaseModel
# from sqlalchemy.orm import Session

# try:                                    # .env support is optional
#     from dotenv import load_dotenv
#     load_dotenv()
# except ImportError:
#     pass


# # ==========================================================================================
# # PART 1 - SCHEDULING ENGINE (pure python)
# # ==========================================================================================
# """
# Rules implemented
# * Routing is strictly sequential per production order (PO): operation k+1 starts only after
#   EVERY piece of operation k has finished (plus cooldown / next-day release if configured).
# * SERIES operation : one task for the whole PO quantity, time = qty x time-per-PCB.
# * BATCH  operation : PO quantity is cut into batches of at most `batch_size` PCBs (max 30);
#                      each batch is a separate task (time = time-per-batch) and the batches
#                      are spread over the parallel machines / operators of that stage.
# * MACHINE stages use machines, MANUAL stages use operators as the resource.
# * Working time comes from SHIFTS (switchable / extendable per date range, per operator).
# * Interlocks are time windows in which a resource cannot be used (breakdown, planned
#   maintenance, spare / consumable out of stock, ...) and operator qualification expiry.
# """

# Interval = Tuple[datetime, datetime]
# WinFn = Callable[[date], List[Interval]]
# FAR_FUTURE = datetime(2999, 1, 1)

# DEFAULT_RULES: Dict[str, Any] = {
#     "max_batch_size": 30,        # a batch can never hold more PCBs than this
#     "default_batch_size": 30,
#     "atomic_batches": True,      # a batch is not split over a shift break if it fits in one working piece
#     "atomic_limit_minutes": 480,
#     "horizon_days": 400,         # how far ahead a task may be searched
# }


# class NoSlot(Exception):
#     """The task cannot be placed on this resource (blocked for ever / beyond the horizon)."""


# # ---------------------------------------- interval helpers --------------------------------
# def _merge_min(iv: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
#     out: List[Tuple[int, int]] = []
#     for s, e in sorted(iv):
#         if e <= s:
#             continue
#         if out and s <= out[-1][1]:
#             out[-1] = (out[-1][0], max(out[-1][1], e))
#         else:
#             out.append((s, e))
#     return out


# def merge_dt(iv: List[Interval]) -> List[Interval]:
#     out: List[Interval] = []
#     for s, e in sorted(iv):
#         if e <= s:
#             continue
#         if out and s <= out[-1][1]:
#             out[-1] = (out[-1][0], max(out[-1][1], e))
#         else:
#             out.append((s, e))
#     return out


# def union_fn(fns: List[WinFn]) -> WinFn:
#     def f(d: date) -> List[Interval]:
#         iv: List[Interval] = []
#         for fn in fns:
#             iv += fn(d)
#         return merge_dt(iv)
#     return f


# def intersect_fn(fa: WinFn, fb: WinFn) -> WinFn:
#     """Windows of A that are also inside windows of B (B is looked at +-1 day for night shifts)."""
#     def f(d: date) -> List[Interval]:
#         b: List[Interval] = []
#         for k in (-1, 0, 1):
#             b += fb(d + timedelta(days=k))
#         b = merge_dt(b)
#         out: List[Interval] = []
#         for a0, a1 in fa(d):
#             for b0, b1 in b:
#                 s, e = max(a0, b0), min(a1, b1)
#                 if s < e:
#                     out.append((s, e))
#         return merge_dt(out)
#     return f


# def iter_windows(win_fn: WinFn, start: datetime, horizon_days: int = 400) -> Iterator[Interval]:
#     """Merged working intervals ending after `start`, in time order (first one is clipped to `start`)."""
#     pending: Optional[Interval] = None
#     first = start.date() - timedelta(days=1)       # a night shift of the day before may still be running
#     for i in range(horizon_days + 2):
#         for ws, we in win_fn(first + timedelta(days=i)):
#             if we <= start:
#                 continue
#             ws = max(ws, start)
#             if pending and ws <= pending[1]:
#                 pending = (pending[0], max(pending[1], we))
#             else:
#                 if pending:
#                     yield pending
#                 pending = (ws, we)
#     if pending:
#         yield pending


# def _free_pieces(ws: datetime, we: datetime, blocks: List[Tuple[datetime, datetime]]) -> Iterator[Interval]:
#     cur = ws
#     for bs, be in blocks:
#         if be <= cur:
#             continue
#         if bs >= we:
#             break
#         if bs > cur:
#             yield cur, bs
#         cur = max(cur, be)
#         if cur >= we:
#             return
#     if cur < we:
#         yield cur, we


# def place(win_fn: WinFn, blocks: List[Tuple[datetime, datetime]], start: datetime, minutes: float,
#           atomic_limit: float = 0, horizon_days: int = 400) -> Tuple[List[Interval], datetime]:
#     """
#     Put `minutes` of work on a resource, starting no earlier than `start`, using only its working
#     windows minus its blocked intervals.  Returns (working pieces, end).
#     atomic_limit > 0 : if the job is not longer than this it must fit in ONE piece (no pause inside).
#     """
#     minutes = max(float(minutes), 1.0)
#     blocks = sorted(blocks)
#     dead_after = min((bs for bs, be in blocks if be >= FAR_FUTURE), default=None)   # blocked for ever from here
#     atomic = 0 < minutes <= atomic_limit
#     segs: List[Interval] = []
#     remaining = minutes
#     for ws, we in iter_windows(win_fn, start, horizon_days):
#         if dead_after is not None and ws >= dead_after:
#             break
#         for ps, pe in _free_pieces(ws, we, blocks):
#             length = (pe - ps).total_seconds() / 60.0
#             if atomic and length + 1e-9 < remaining:
#                 continue                                  # too short for the whole job: wait for a longer piece
#             use = min(length, remaining)
#             segs.append((ps, ps + timedelta(minutes=use)))
#             remaining -= use
#             if remaining <= 1e-9:
#                 return segs, segs[-1][1]
#     raise NoSlot()


# def clip_to_windows(win_fn: WinFn, s: datetime, e: datetime) -> List[Interval]:
#     """Working pieces of the span [s, e] (used for history that already happened)."""
#     out = [(max(a, s), min(b, e)) for a, b in iter_windows(win_fn, s) if a < e]
#     out = [(a, b) for a, b in out if a < b]
#     return out or [(s, e)]


# # ---------------------------------------- shifts + calendar -------------------------------
# @dataclass
# class ShiftDef:
#     name: str
#     start_min: int                 # minutes after midnight
#     end_min: int                   # may be > 1440 for a night shift
#     active: bool = True            # normally running?


# @dataclass
# class ShiftChange:
#     """Runtime adjustment valid for date_from..date_to (inclusive)."""
#     date_from: date
#     date_to: date
#     action: str                    # 'disable' | 'enable' | 'add'
#     shift_name: Optional[str] = None   # disable/enable: which shift (None = every shift, 'disable' only)
#     start_min: Optional[int] = None    # add: custom hours (extra shift / overtime)
#     end_min: Optional[int] = None
#     operator: Optional[str] = None     # None = whole plant, else operator staff no
#     note: str = ""


# class Calendar:
#     def __init__(self, public_holidays=(), exceptions: Optional[Dict[date, str]] = None,
#                  shifts: Optional[List[ShiftDef]] = None, changes: Optional[List[ShiftChange]] = None,
#                  operator_shifts: Optional[Dict[str, List[str]]] = None, alt_saturday_off: bool = True):
#         self.public_holidays = set(public_holidays)
#         self.exceptions = {d: (t or "").strip().lower() for d, t in (exceptions or {}).items()}
#         self.shifts = list(shifts or [ShiftDef("Shift 1", 360, 780), ShiftDef("Shift 2", 780, 1200)])
#         self.changes = list(changes or [])
#         self.operator_shifts = {k: set(v) for k, v in (operator_shifts or {}).items() if v}
#         self.alt_saturday_off = alt_saturday_off
#         self._cache: Dict[Tuple[date, Optional[str]], List[Tuple[int, int]]] = {}

#     # 1 = full day, 0.5 = half day, 0 = off
#     def day_factor(self, d: date) -> float:
#         exc = self.exceptions.get(d)
#         if exc == "working":
#             return 1.0
#         if exc == "half-day":
#             return 0.5
#         if exc == "holiday" or d in self.public_holidays or d.weekday() == 6:
#             return 0.0
#         if self.alt_saturday_off and d.weekday() == 5 and d.month >= 4 and ((d.day - 1) // 7 + 1) in (1, 3):
#             return 0.0
#         return 1.0

#     def _minutes(self, d: date, operator: Optional[str]) -> List[Tuple[int, int]]:
#         key = (d, operator)
#         if key in self._cache:
#             return self._cache[key]
#         by_name = {s.name: s for s in self.shifts}
#         factor = self.day_factor(d)
#         active = {s.name for s in self.shifts if s.active} if factor > 0 else set()
#         custom: List[Tuple[int, int]] = []

#         def apply(who: Optional[str]):
#             for ch in self.changes:
#                 if ch.operator != who or not (ch.date_from <= d <= ch.date_to):
#                     continue
#                 if ch.action == "disable":
#                     if ch.shift_name is None:
#                         active.clear()
#                         custom.clear()
#                     else:
#                         active.discard(ch.shift_name)
#                 elif ch.action == "enable" and ch.shift_name in by_name:
#                     active.add(ch.shift_name)
#                 elif ch.action == "add" and ch.start_min is not None and ch.end_min is not None:
#                     end = ch.end_min if ch.end_min > ch.start_min else ch.end_min + 1440
#                     custom.append((ch.start_min, end))

#         apply(None)
#         if operator is not None:
#             assigned = self.operator_shifts.get(operator)
#             if assigned:
#                 active &= assigned
#             apply(operator)

#         iv = _merge_min([(by_name[n].start_min, by_name[n].end_min) for n in active] + custom)
#         if factor == 0.5 and iv:                         # half day = first half of the working minutes
#             keep = sum(e - s for s, e in iv) / 2.0
#             trimmed = []
#             for s, e in iv:
#                 take = min(e - s, keep)
#                 if take > 0:
#                     trimmed.append((s, int(s + take)))
#                 keep -= take
#             iv = trimmed
#         self._cache[key] = iv
#         return iv

#     def windows(self, d: date, operator: Optional[str] = None) -> List[Interval]:
#         base = datetime(d.year, d.month, d.day)
#         return [(base + timedelta(minutes=s), base + timedelta(minutes=e)) for s, e in self._minutes(d, operator)]

#     def plant_fn(self) -> WinFn:
#         return lambda d: self.windows(d)

#     def operator_fn(self, staff_no: str) -> WinFn:
#         return lambda d: self.windows(d, staff_no)

#     def next_working_start(self, dt: datetime) -> datetime:
#         for ws, _ in iter_windows(self.plant_fn(), dt):
#             return ws
#         raise RuntimeError("No working time found - check shifts / holidays")

#     def add_working_minutes(self, start: datetime, minutes: float) -> datetime:
#         return place(self.plant_fn(), [], start, minutes)[1]

#     def day_windows_json(self, d_from: date, d_to: date) -> Tuple[Dict[str, List[List[int]]], float, float]:
#         """Working windows per calendar day (minutes after midnight, night shifts cut at midnight) for the
#         front-end, plus the hull (first start hour, last end hour) of everything in the range."""
#         out: Dict[str, List[List[int]]] = {}
#         lo, hi, n = 1440, 0, (d_to - d_from).days
#         fn = self.plant_fn()
#         for i in range(-1, n + 2):
#             d = d_from + timedelta(days=i)
#             for ws, we in fn(d):
#                 cur = ws
#                 while cur < we:
#                     day_end = datetime(cur.year, cur.month, cur.day) + timedelta(days=1)
#                     piece_end = min(we, day_end)
#                     day = cur.date()
#                     if d_from <= day <= d_to:
#                         s = cur.hour * 60 + cur.minute
#                         e = 1440 if piece_end == day_end else piece_end.hour * 60 + piece_end.minute
#                         out.setdefault(day.isoformat(), []).append([s, e])
#                         lo, hi = min(lo, s), max(hi, e)
#                     cur = piece_end
#         for k in out:
#             out[k] = [list(x) for x in _merge_min([tuple(x) for x in out[k]])]
#         if hi == 0:
#             return out, 6.0, 20.0
#         return out, math.floor(lo / 60.0), math.ceil(hi / 60.0)


# # ---------------------------------------- resources ---------------------------------------
# @dataclass
# class Resource:
#     id: str
#     name: str
#     kind: str                                   # MACHINE | MANUAL | FALLBACK
#     work_centre: str
#     stage_id: Optional[int]
#     win_fn: WinFn
#     blocks: List[Tuple[datetime, Optional[datetime], str]] = field(default_factory=list)  # (from, to|None, reason)
#     operator: Optional[str] = None              # staff no (MANUAL)
#     valid_until: Dict[int, datetime] = field(default_factory=dict)   # stage_id -> qualification expiry
#     meta: Dict[str, Any] = field(default_factory=dict)

#     def hard_blocks(self) -> List[Tuple[datetime, datetime]]:
#         return sorted((s, e or FAR_FUTURE) for s, e, _ in self.blocks)

#     def reasons_near(self, s: datetime, e: datetime) -> List[str]:
#         out = [r for bs, be, r in self.blocks if bs < e and (be or FAR_FUTURE) > s]
#         return out or [r for bs, be, r in self.blocks if be is None]


# # ---------------------------------------- scheduler ---------------------------------------
# def _release_after(cal: Calendar, op: Dict[str, Any], end: datetime) -> datetime:
#     """Earliest moment the NEXT operation of the order may start (cooldown, next-day release)."""
#     ready = end
#     cd = float(op.get("cooldown_minutes") or 0)
#     if cd > 0:
#         ready = cal.add_working_minutes(end, cd) if op.get("cooldown_working_only") else end + timedelta(minutes=cd)
#     if op["mode"] == "BATCH" and op.get("next_day_release"):
#         ready = max(ready, datetime.combine(end.date() + timedelta(days=1), time(0)))
#     return ready


# def batch_split(op: Dict[str, Any], rules: Dict[str, Any]) -> List[Tuple[int, float]]:
#     """[(pcbs, minutes)] for every task of the operation."""
#     qty = int(op["quantity"])
#     if op["mode"] == "BATCH":
#         size = int(op.get("batch_size") or rules["default_batch_size"])
#         size = max(1, min(size, rules["max_batch_size"]))
#         n = max(1, math.ceil(qty / size))
#         return [(min(size, qty - size * k), float(op["unit_minutes"])) for k in range(n)]
#     return [(qty, max(qty * float(op["unit_minutes"]), 1.0))]


# def run_scheduler(cal: Calendar, stage_resources: Dict[int, List[Resource]], orders: Dict[str, Dict[str, Any]],
#                   sequence: List[str], fixed: Optional[Dict[str, Dict[str, Any]]] = None,
#                   pins: Optional[Dict[str, Dict[str, Any]]] = None, not_before: Optional[datetime] = None,
#                   rules: Optional[Dict[str, Any]] = None) -> Dict[str, List[Dict[str, Any]]]:
#     """
#     orders[po] = {"release": datetime, "part_number": str, "ops": [op, ...]} ; op keys:
#         op_id, flow_step_id, mode ('SERIES'|'BATCH'), quantity, unit_minutes (per PCB / per batch),
#         batch_size, setup_minutes, cooldown_minutes, cooldown_working_only, next_day_release
#     fixed[op_id] : work that already happened / is running
#                    {resource_id, start, end}                (finished)
#                    {resource_id, start, remaining_minutes}  (running)
#                    optional "confirmed": True when resource_id is the machine really used
#     pins[task_id]: manual move of one task                   {resource_id, start}
#     returns {op_id: [task, ...]}
#     """
#     rules = {**DEFAULT_RULES, **(rules or {})}
#     by_id = {r.id: r for lst in stage_resources.values() for r in lst}
#     state: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"free": datetime.min, "last_part": None})
#     result: Dict[str, List[Dict[str, Any]]] = {}
#     horizon = rules["horizon_days"]
#     nb = not_before or datetime.min

#     # 1. Manual moves (pins) are reserved first: they keep their slot and everything else is planned around them.
#     reserved: Dict[str, List[Tuple[datetime, datetime]]] = defaultdict(list)
#     pin_slots: Dict[str, Tuple[Resource, List[Interval], datetime]] = {}
#     for po_no in sequence:
#         for op in orders[po_no]["ops"]:
#             if (fixed and op["op_id"] in fixed) or not pins:
#                 continue
#             pieces = batch_split(op, rules)
#             for k, (_, minutes) in enumerate(pieces, start=1):
#                 tid = op["op_id"] if len(pieces) == 1 else f"{op['op_id']}-B{k}"
#                 pin = pins.get(tid)
#                 if pin and pin.get("start") and pin.get("resource_id") in by_id:
#                     res = by_id[pin["resource_id"]]
#                     atomic = rules["atomic_limit_minutes"] if (op["mode"] == "BATCH" and rules["atomic_batches"]) else 0
#                     try:
#                         segs, end = place(res.win_fn, [], pin["start"], minutes, atomic, horizon)
#                     except NoSlot:
#                         continue
#                     pin_slots[tid] = (res, segs, end)
#                     reserved[res.id] += segs

#     # 2. FIX: running / finished work is reserved BEFORE new work is planned, so new work goes around it.
#     #    A resource does one job at a time: the remaining work of several started POs on the same operator /
#     #    machine is queued one after the other (the PO started first finishes first), not run in parallel.
#     #    If the resource of running work is only a guess, the stage resource that finishes it first is used.
#     fixed_slots: Dict[str, Tuple[Resource, datetime, datetime, List[Interval], List[Interval]]] = {}
#     if fixed:
#         todo = [op for po_no in sequence for op in orders[po_no]["ops"] if op["op_id"] in fixed]
#         todo.sort(key=lambda o: ("remaining_minutes" in fixed[o["op_id"]], fixed[o["op_id"]]["start"]))
#         for op in todo:
#             fx = fixed[op["op_id"]]
#             cands = stage_resources[op["flow_step_id"]]
#             res = by_id.get(fx["resource_id"]) or cands[0]
#             s = fx["start"]
#             new: List[Interval] = []
#             if "remaining_minutes" in fx:
#                 from_t = max(nb, s)
#                 mins = fx["remaining_minutes"]
#                 options = [res] if fx.get("confirmed") else [res] + [r for r in cands if r is not res]
#                 best = None
#                 for r in options:
#                     try:
#                         got = place(r.win_fn, reserved.get(r.id, []), from_t, mins, 0, horizon)
#                     except NoSlot:
#                         continue
#                     if best is None or got[1] < best[1][1]:
#                         best = (r, got)
#                 if best:
#                     res, (new, e) = best
#                 else:                                     # resource has no working time at all: show it anyway
#                     new = [(from_t, from_t + timedelta(minutes=max(mins, 1.0)))]
#                     e = new[-1][1]
#                 segs = (clip_to_windows(res.win_fn, s, from_t) if from_t > s else []) + new
#             else:
#                 e = max(fx["end"], s)
#                 segs = clip_to_windows(res.win_fn, s, e) if e > s else [(s, e)]
#             fixed_slots[op["op_id"]] = (res, s, e, segs, new)
#             reserved[res.id] += [(a, b) for a, b in segs if b > nb]   # only the future part blocks the machine

#     # 3. Everything else, PO by PO in sequence order, operation by operation.
#     for po_no in sequence:
#         po = orders[po_no]
#         part = po["part_number"]
#         ready = po["release"]
#         for op in po["ops"]:
#             lower = ready if not_before is None else max(ready, not_before)
#             tasks: List[Dict[str, Any]]

#             if op["op_id"] in fixed_slots:                                 # history / running work
#                 res, s, e, segs, new = fixed_slots[op["op_id"]]
#                 tasks = [_task(op, 1, 1, op["quantity"], res, s, e, segs, 0.0, status_fixed=True)]
#                 tasks[0]["new_segments"] = new
#                 state[res.id]["free"] = max(state[res.id]["free"], e)
#                 state[res.id]["last_part"] = part
#             else:
#                 tasks = _schedule_op(cal, op, part, lower, stage_resources, by_id, state, pins or {}, rules,
#                                      reserved, pin_slots)

#             result[op["op_id"]] = tasks
#             ready = _release_after(cal, op, max(t["end"] for t in tasks))
#     return result


# def _task(op, no, count, qty, res: Resource, start, end, segs, setup, status_fixed=False, pinned=False,
#           blocked=False, reasons=None, waiting="") -> Dict[str, Any]:
#     tid = op["op_id"] if count == 1 else f"{op['op_id']}-B{no}"
#     work = sum((b - a).total_seconds() for a, b in segs) / 60.0
#     return {"task_id": tid, "op_id": op["op_id"], "resource_id": res.id, "start": start, "end": end,
#             "segments": segs, "qty": qty, "batch_no": no, "batch_count": count, "setup_minutes": round(setup, 1),
#             "working_minutes": round(work, 1), "fixed": status_fixed, "pinned": pinned,
#             "blocked": blocked, "block_reasons": reasons or [], "waiting_reason": waiting}


# def _schedule_op(cal, op, part, lower, stage_resources, by_id, state, pins, rules, reserved,
#                  pin_slots) -> List[Dict[str, Any]]:
#     cands = stage_resources[op["flow_step_id"]]
#     sid = op["flow_step_id"]
#     pieces = batch_split(op, rules)
#     count = len(pieces)
#     atomic = rules["atomic_limit_minutes"] if (op["mode"] == "BATCH" and rules["atomic_batches"]) else 0
#     horizon = rules["horizon_days"]
#     tasks: List[Dict[str, Any]] = []

#     def try_res(res: Resource, lb: datetime, minutes: float, honour_blocks: bool, honour_expiry: bool):
#         st = state[res.id]
#         start_lb = max(lb, st["free"])
#         setup = float(op.get("setup_minutes") or 0) if st["last_part"] != part else 0.0
#         try:
#             blocks = (res.hard_blocks() if honour_blocks else []) + reserved.get(res.id, [])
#             segs, end = place(res.win_fn, blocks, start_lb, minutes + setup, atomic, horizon)
#         except NoSlot:
#             return None
#         exp = res.valid_until.get(sid)
#         if honour_expiry and exp and segs[0][0] >= exp:
#             return None
#         return segs, end, setup

#     for k, (qty, minutes) in enumerate(pieces, start=1):
#         tid = op["op_id"] if count == 1 else f"{op['op_id']}-B{k}"
#         pin = pins.get(tid)
#         chosen = None                      # (resource, segments, end, setup)
#         blocked = False
#         pinned_slot = False

#         if tid in pin_slots:                                  # supervisor moved this task: keep its reserved slot
#             res, segs, end = pin_slots[tid]
#             chosen = (res, segs, end, 0.0)
#             pinned_slot = True
#         elif pin and pin.get("resource_id") in by_id:        # machine chosen by hand, time left to the planner
#             res = by_id[pin["resource_id"]]
#             got = try_res(res, lower, minutes, False, False)
#             if got:
#                 chosen = (res,) + got
#         if chosen is None:
#             # 1st try: respect every interlock; 2nd: ignore interlocks; 3rd: also ignore operator expiry.
#             for honour_blocks, honour_expiry in ((True, True), (False, True), (False, False)):
#                 best = None
#                 for res in cands:
#                     got = try_res(res, lower, minutes, honour_blocks, honour_expiry)
#                     if got and (best is None or (got[1], got[0][0][0]) < (best[2], best[1][0][0])):
#                         best = (res,) + got
#                 if best:
#                     chosen, blocked = best, not (honour_blocks and honour_expiry)
#                     break
#         if chosen is None:
#             raise RuntimeError(f"Operation {op['op_id']} cannot be placed inside the horizon")

#         res, segs, end, setup = chosen
#         start = segs[0][0]
#         reasons: List[str] = []
#         if blocked:
#             reasons = res.reasons_near(start, end)
#             if not honour_expiry_ok(res, sid, start):
#                 reasons.append("No operator with a valid qualification for this step")
#         st = state[res.id]
#         waiting = ""
#         if start > lower + timedelta(minutes=1):
#             waiting = (f"Waiting for {res.name} (busy until {st['free']:%d %b %H:%M})" if st["free"] > lower
#                        else "Waiting for working time (shift / holiday / interlock)")
#         if not pinned_slot:                                   # a reserved slot does not push the resource's free time
#             st["free"] = max(st["free"], end)
#         st["last_part"] = part
#         tasks.append(_task(op, k, count, qty, res, start, end, segs, setup, pinned=bool(pin), blocked=blocked,
#                            reasons=reasons, waiting=waiting))
#     return tasks


# def honour_expiry_ok(res: Resource, stage_id: int, start: datetime) -> bool:
#     exp = res.valid_until.get(stage_id)
#     return not (exp and start >= exp)


# # ------------------------ post checks (red notes in the Gantt tooltip, never stop the plan) ------------
# def detect_conflicts(orders: Dict[str, Dict[str, Any]], result: Dict[str, List[Dict[str, Any]]],
#                      by_id: Dict[str, Resource]) -> Dict[str, List[str]]:
#     notes: Dict[str, List[str]] = defaultdict(list)

#     # 1. sequence: every task must start after the previous operation has finished
#     for po in orders.values():
#         prev_end, prev_name = None, ""
#         for op in po["ops"]:
#             tasks = result.get(op["op_id"], [])
#             if prev_end is not None:
#                 for t in tasks:
#                     if t.get("history"):
#                         continue                              # real serial flow: stages overlap per PO
#                     if t["start"] + timedelta(minutes=1) < prev_end:
#                         notes[t["task_id"]].append(f"Starts before '{prev_name}' of the same order has finished")
#             if tasks:
#                 prev_end, prev_name = max(t["end"] for t in tasks), op["step_name"]

#     # 2. one resource cannot do two things at once;  3. interlock windows
#     per_res: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
#     for tasks in result.values():
#         for t in tasks:
#             per_res[t["resource_id"]].append(t)
#     for rid, lst in per_res.items():
#         lst.sort(key=lambda t: t["start"])
#         for a, b in zip(lst, lst[1:]):
#             if b["start"] + timedelta(minutes=1) < a["end"] and any(
#                     x[0] < y[1] and y[0] < x[1] for x in a["segments"] for y in b["segments"]):
#                 notes[a["task_id"]].append(f"Overlaps {b['task_id']} on the same resource")
#                 notes[b["task_id"]].append(f"Overlaps {a['task_id']} on the same resource")
#         res = by_id.get(rid)
#         if res:
#             for t in lst:
#                 if t["blocked"] or t.get("history"):
#                     continue                                  # provisional / already happened
#                 for bs, be, reason in res.blocks:
#                     if any(s < (be or FAR_FUTURE) and bs < e for s, e in t["segments"]):
#                         notes[t["task_id"]].append(f"Runs during interlock: {reason}")
#     return notes


# # ==========================================================================================
# # PART 2 - DATABASE LAYER (reads the MES tables, builds the plan)
# # ==========================================================================================
# from Database import session as SessionLocal, engine as db_engine          # noqa: E402
# from Database_models import (                                               # noqa: E402
#     base, CalendarException, Consumables, Equipments, InventorySpares, MachineBreakdownLog,
#     MaintenanceScheduleLog, OperatorLog, OperatorShiftAssignment, OperatorStepMapping, PartProcessTime,
#     PcbData, ProcessFlowMaster, Processtype, ProductionOrderTracking, ProductTypeMaster,
#     ProductWorkflowMapping, PublicHoliday, ScheduleOverride, ShiftAdjustment, ShiftDefinition,
#     SPCDataLog, StageCapacityDB, StageScheduleConfig,
# )

# # ---------------------------------------- settings you may want to tune -------------------
# ISO = "%Y-%m-%dT%H:%M:%S"
# COMPLETED_STATUSES = {"COMPLETED", "COMPLETE", "DONE", "FINISHED"}
# CLOSED_PO_STATUSES = {"completed", "closed", "cancelled", "canceled"}
# RULES = {"max_batch_size": 30, "default_batch_size": 30}      # max PCBs per batch = 30
# PM_DOWNTIME_HOURS = 8               # a planned preventive-maintenance day blocks the machine for this long
# BLOCK_WHEN_BELOW_THRESHOLD = False  # False: spare/consumable blocks the machine only when qty_avl <= 0
# MRL_WARN_DAYS = 30                  # warn when an operator qualification expires within this many days
# CONSUMABLE_WARN_DAYS = 7            # warn when the last usable consumable lot expires within this many days
# SHARE_MACHINE_ACROSS_STAGES = True  # one equipment mapped to several stages = one resource (no double booking)


# def _log_shift_minutes() -> float:
#     v = (os.getenv("OPERATOR_LOG_SHIFT_MINUTES", "auto") or "auto").strip().lower()
#     if v == "auto":                                  # operator_log is written in UTC -> this server's local time
#         off = datetime.now().astimezone().utcoffset()
#         return off.total_seconds() / 60.0 if off else 0.0
#     return float(v)


# OPERATOR_LOG_SHIFT_MINUTES = _log_shift_minutes()
# STALE_RUNNING_HOURS = float(os.getenv("STALE_RUNNING_HOURS", "12") or 12)
# HISTORY_MERGE_GAP_MINUTES = float(os.getenv("HISTORY_MERGE_GAP_MINUTES", "30") or 30)
# RUNNING_STATUSES = {"STARTED", "START", "REWORK_STARTED", "IN_PROGRESS", "RUNNING", "RESUMED"}
# DEFAULT_CFG = {"batch_size": 30, "next_day_release": False, "cooldown_minutes": 0.0,
#                "cooldown_working_time_only": False, "setup_minutes": 0.0, "resource_mode": "AUTO",
#                "needs_operator": False}


# # ---------------------------------------- small helpers -----------------------------------
# def _iso(dt: datetime) -> str:
#     return dt.strftime(ISO)


# def _hm(minutes: float) -> str:
#     m = int(round(minutes)) % 1440
#     return f"{m // 60:02d}:{m % 60:02d}"


# def _to_min(hhmm: str) -> int:
#     h, m = str(hhmm).strip().split(":")[:2]
#     h, m = int(h), int(m)
#     if not (0 <= h <= 24 and 0 <= m < 60):
#         raise ValueError(hhmm)
#     return h * 60 + m


# def _num(v) -> Optional[float]:
#     if v is None:
#         return None
#     m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
#     return float(m.group()) if m else None


# def _days(v) -> Optional[float]:
#     n = _num(v)
#     if n is None:
#         return None
#     t = str(v).lower()
#     return n * 7 if "week" in t else n * 30 if "month" in t else n


# _DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d.%m.%Y", "%d %b %Y", "%m/%d/%Y")


# def _parse_date(v) -> Optional[date]:
#     if not v:
#         return None
#     if isinstance(v, datetime):
#         return v.date()
#     if isinstance(v, date):
#         return v
#     s = str(v).strip().replace("Z", "")
#     try:
#         return datetime.fromisoformat(s).date()
#     except ValueError:
#         pass
#     for f in _DATE_FORMATS:
#         try:
#             return datetime.strptime(s[:11].strip() if "%b" in f else s[:10], f).date()
#         except ValueError:
#             continue
#     return None


# def _parse_dt(date_s, time_s=None) -> Optional[datetime]:
#     if not date_s:
#         return None
#     if isinstance(date_s, datetime):
#         return date_s
#     try:
#         return datetime.fromisoformat(str(date_s).replace("Z", "").strip())
#     except ValueError:
#         pass
#     d = _parse_date(date_s)
#     if not d:
#         return None
#     t = datetime.min
#     for f in ("%H:%M:%S", "%H:%M", "%I:%M %p"):
#         try:
#             t = datetime.strptime(str(time_s or "00:00").strip(), f)
#             break
#         except ValueError:
#             continue
#     return datetime(d.year, d.month, d.day, t.hour, t.minute)


# def _parse_mrl_expiry(v) -> Optional[datetime]:
#     """
#     FIX: operator_MRL_Expiry is stored as MMYY ("0127", or "127.0" after an Excel import). The rest of the
#     app reads it with strptime("%m%y"), i.e. the qualification is valid until the 1st of that month.
#     Full dates are still accepted (valid to the end of that day).
#     """
#     if v is None:
#         return None
#     s = str(v).strip()
#     if not s or s.lower() in ("nan", "none", "null"):
#         return None
#     if s.endswith(".0"):
#         s = s[:-2]
#     if s.isdigit() and len(s) in (3, 4):
#         try:
#             return datetime.strptime(s.zfill(4), "%m%y")
#         except ValueError:
#             return None
#     d = _parse_date(s)
#     return datetime(d.year, d.month, d.day) + timedelta(days=1) if d else None


# def _as_log_time(v) -> Optional[datetime]:
#     """operator_log time -> local datetime (applies OPERATOR_LOG_SHIFT_MINUTES)."""
#     dt = v if isinstance(v, datetime) else _parse_dt(v)
#     if dt is None:
#         return None
#     if dt.tzinfo is not None:
#         dt = dt.replace(tzinfo=None)
#     return dt + timedelta(minutes=OPERATOR_LOG_SHIFT_MINUTES)


# def _norm(s) -> str:
#     s = str(s or "").strip().lower()
#     return s[:-2] if s.endswith(".0") else s


# def _norm_staff(v) -> Optional[str]:
#     v = (str(v).strip() if v is not None else "")
#     return v or None


# def _staff_key(s) -> str:
#     """'E0169', 'e169', '169' -> '169' so operator_log and operator_step_mapping staff numbers match."""
#     return _norm(s).lstrip("e").lstrip("0") or "0"


# def _machine_from_log_data(data, aliases: Optional[Dict[str, str]]) -> Optional[str]:
#     """Machine id recorded in a dynamic form (any top-level key containing machine / eqpt / equip)."""
#     if not isinstance(data, dict) or not aliases:
#         return None
#     for k, v in data.items():
#         if not any(t in str(k).lower() for t in ("machine", "eqpt", "equip")):
#             continue
#         for x in (v if isinstance(v, list) else [v]):
#             if isinstance(x, dict):
#                 x = x.get("selectedValue") or x.get("value")
#             rid = aliases.get(_norm(x))
#             if rid:
#                 return rid
#     return None


# # ---------------------------------------- calendar / shifts -------------------------------
# def _legacy_shifts(caps) -> List[ShiftDef]:
#     """No rows in shift_definition: derive the shifts from stage_capacity exactly as before."""
#     hps = [float(c.hours_per_shift) for c in caps if c.hours_per_shift and float(c.hours_per_shift) > 0]
#     spd = [int(c.shifts_per_day) for c in caps if c.shifts_per_day and int(c.shifts_per_day) > 0]
#     start_h = (getattr(caps[0], "shift_start_hour", None) or 6) if caps else 6
#     per = max(hps) if hps else 7.0
#     n = max(spd) if spd else 2
#     work = min(per * n, 24 - start_h)
#     each = work / n
#     return [ShiftDef(f"Shift {k + 1}", int(round((start_h + k * each) * 60)),
#                      int(round((start_h + (k + 1) * each) * 60)), True) for k in range(n)]


# def _load_shifts(db: Session, caps) -> Tuple[List[ShiftDef], bool]:
#     rows = db.query(ShiftDefinition).order_by(ShiftDefinition.sort_order, ShiftDefinition.id).all()
#     if not rows:
#         return _legacy_shifts(caps), True
#     out = []
#     for r in rows:
#         s, e = _to_min(r.start_time), _to_min(r.end_time)
#         out.append(ShiftDef(r.name, s, e if e > s else e + 1440, bool(r.is_active)))
#     return out, False


# def _load_changes(db: Session) -> List[ShiftChange]:
#     out = []
#     for r in db.query(ShiftAdjustment).order_by(ShiftAdjustment.id).all():
#         out.append(ShiftChange(
#             date_from=r.date_from, date_to=r.date_to or r.date_from, action=(r.action or "").lower(),
#             shift_name=r.shift_name or None,
#             start_min=_to_min(r.start_time) if r.start_time else None,
#             end_min=_to_min(r.end_time) if r.end_time else None,
#             operator=_norm_staff(r.operator_staff_no), note=r.note or ""))
#     return out


# def _calendar_json(cal: Calendar, public_hols, exc_rows, today: date) -> Dict[str, Any]:
#     all_dates = [e.exception_date for e in exc_rows] + [h.holiday_date for h in public_hols]
#     past = max(60, (today - min(all_dates)).days) if all_dates else 60
#     future = max(240, (max(all_dates) - today).days) if all_dates else 240
#     d_from, d_to = today - timedelta(days=past), today + timedelta(days=future)

#     holidays: Dict[str, Dict[str, Any]] = {}
#     for h in public_hols:
#         holidays[h.holiday_date.isoformat()] = {"holiday_date": h.holiday_date.isoformat(), "name": h.name,
#                                                 "year": h.holiday_date.year}
#     working_exceptions = []
#     for e in exc_rows:
#         etype, key = (e.exception_type or "").strip().lower(), e.exception_date.isoformat()
#         if etype == "holiday" and key not in holidays:
#             holidays[key] = {"holiday_date": key, "name": e.note or "Holiday", "year": e.exception_date.year}
#         elif etype in ("working", "half-day"):
#             holidays.pop(key, None)
#             working_exceptions.append({"date": key, "type": e.exception_type, "note": e.note})
#     d = d_from
#     while d <= d_to:
#         if cal.day_factor(d) == 0 and d.weekday() == 5 and d.isoformat() not in holidays:
#             holidays[d.isoformat()] = {"holiday_date": d.isoformat(), "name": "Saturday off (1st/3rd)", "year": d.year}
#         d += timedelta(days=1)

#     day_windows, lo, hi = cal.day_windows_json(d_from, d_to)
#     return {
#         "shift_start": lo, "shift_end": hi,           # hull of every working window (drives the daily/weekly axis)
#         "shifts": [{"name": s.name, "start": _hm(s.start_min), "end": _hm(s.end_min), "active": s.active}
#                    for s in cal.shifts],
#         "week_off": [0],
#         "holidays": [holidays[k] for k in sorted(holidays)],
#         "working_exceptions": working_exceptions,
#         "day_windows": day_windows,                   # {"2026-10-13": [[360, 1320]]} minutes after midnight
#     }


# # ---------------------------------------- resources + interlocks --------------------------
# def _stage_cfg(db: Session) -> Dict[int, Dict[str, Any]]:
#     out = {}
#     for r in db.query(StageScheduleConfig).all():
#         out[r.stage_id] = {
#             "batch_size": int(r.batch_size or DEFAULT_CFG["batch_size"]),
#             "next_day_release": bool(r.next_day_release),
#             "cooldown_minutes": float(r.cooldown_minutes or 0),
#             "cooldown_working_time_only": bool(r.cooldown_working_time_only),
#             "setup_minutes": float(r.setup_minutes or 0),
#             "resource_mode": (r.resource_mode or "AUTO").upper(),
#             "needs_operator": bool(r.needs_operator),
#         }
#     return out


# def _build_resources(db, cal, stages, cfg, qualified, today):
#     """-> (resources list in display order, stage_resources, stage_mode, alias map, warnings)"""
#     warnings: List[str] = []
#     plant = cal.plant_fn()
#     equipments = db.query(Equipments).order_by(Equipments.stage_id, Equipments.id).all()
#     machines_by_stage: Dict[int, List[Equipments]] = defaultdict(list)
#     for e in equipments:
#         if e.stage_id is not None:
#             machines_by_stage[e.stage_id].append(e)

#     def mode_of(sid: int) -> str:
#         want = cfg.get(sid, DEFAULT_CFG)["resource_mode"]          # AUTO | MACHINE | MANUAL
#         has_m, has_o = bool(machines_by_stage.get(sid)), bool(qualified.get(sid))
#         if want == "MANUAL" and has_o:
#             return "MANUAL"
#         if want == "MACHINE" and has_m:
#             return "MACHINE"
#         return "MACHINE" if has_m else "MANUAL" if has_o else "FALLBACK"

#     def machine_window(sid: Optional[int]) -> WinFn:
#         if sid is not None and cfg.get(sid, DEFAULT_CFG)["needs_operator"]:
#             ops = [q["staff"] for q in qualified.get(sid, []) if q["staff"]]
#             if ops:
#                 return intersect_fn(plant, union_fn([cal.operator_fn(o) for o in ops]))
#             stage = stages.get(sid)
#             warnings.append(f"Stage '{stage.step_name if stage else sid}' needs an operator but none is qualified")
#         return plant

#     stage_mode = {sid: mode_of(sid) for sid in stages}
#     resources: List[Resource] = []
#     stage_resources: Dict[int, List[Resource]] = defaultdict(list)
#     alias: Dict[str, List[Resource]] = defaultdict(list)
#     by_mid: Dict[str, Resource] = {}

#     # --- machines
#     for e in equipments:
#         if e.stage_id is not None and stage_mode.get(e.stage_id) == "MANUAL":
#             continue
#         mid = e.machine_id or e.eqpt_no or f"EQ-{e.id}"
#         if mid in by_mid:
#             if SHARE_MACHINE_ACROSS_STAGES:                  # FIX: same machine on another stage = same resource
#                 r = by_mid[mid]
#                 if e.stage_id is not None and e.stage_id not in r.meta["stages"]:
#                     r.meta["stages"].add(e.stage_id)
#                     if r.stage_id is None:
#                         r.stage_id = e.stage_id
#                     stage_resources[e.stage_id].append(r)
#                 continue
#             mid = f"{mid}-{e.id}"
#         stage = stages.get(e.stage_id)
#         r = Resource(
#             id=mid, name=e.eqpt_name or mid, kind="MACHINE",
#             work_centre=e.workcenter_name or (stage.step_name if stage else ""), stage_id=e.stage_id,
#             win_fn=machine_window(e.stage_id),
#             meta={"eqpt_no": e.eqpt_no or "", "eqpt_name": e.eqpt_name or "", "eqpt_make": e.eqpt_make or "-",
#                   "eqpt_model": e.eqpt_model or "-", "stages": {e.stage_id}})
#         by_mid[mid] = r
#         resources.append(r)
#         if e.stage_id is not None:
#             stage_resources[e.stage_id].append(r)
#         for a in (e.machine_id, e.eqpt_no):
#             if a:
#                 alias[_norm(a)].append(r)

#     # --- operators (resource of MANUAL stages)
#     op_res: Dict[str, Resource] = {}
#     for sid, mode in stage_mode.items():
#         if mode != "MANUAL":
#             continue
#         for q in qualified.get(sid, []):
#             key = q["staff"] or q["name"]
#             r = op_res.get(key)
#             if r is None:
#                 r = Resource(id=f"OP-{key}", name=q["name"] or key, kind="MANUAL", work_centre="", stage_id=sid,
#                              win_fn=cal.operator_fn(q["staff"]) if q["staff"] else plant,
#                              operator=q["staff"], meta={"stages": set(), "names": []})
#                 op_res[key] = r
#                 resources.append(r)
#                 alias[_norm(key)].append(r)
#             r.meta["stages"].add(sid)
#             r.meta["names"].append(stages[sid].step_name)
#             if q["expiry"]:
#                 r.valid_until[sid] = q["expiry"]
#             stage_resources[sid].append(r)
#     for r in op_res.values():
#         r.work_centre = " / ".join(dict.fromkeys(r.meta["names"]))

#     # --- stages nobody can serve -> visible "unassigned" row so the plan still has dates
#     for sid, mode in stage_mode.items():
#         if mode == "FALLBACK" and sid in stages:
#             nm = stages[sid].step_name
#             warnings.append(f"Stage '{nm}': no machine and no qualified operator - shown as Unassigned")
#             r = Resource(id=f"UNASSIGNED-{sid}", name=f"{nm} (Unassigned)", kind="FALLBACK", work_centre=nm,
#                          stage_id=sid, win_fn=plant, meta={"stages": {sid}})
#             resources.append(r)
#             stage_resources[sid].append(r)
#     return resources, stage_resources, stage_mode, alias, warnings


# def _qualified(db: Session) -> Dict[int, List[Dict[str, Any]]]:
#     out: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
#     seen = set()
#     for o in db.query(OperatorStepMapping).order_by(OperatorStepMapping.step_map_id).all():
#         staff = _norm_staff(o.operator_staff_no)
#         key = (o.flow_step_id, staff or o.operator_name)
#         if not (staff or o.operator_name) or key in seen:
#             continue
#         seen.add(key)
#         out[o.flow_step_id].append({"staff": staff, "name": o.operator_name or staff,
#                                     "expiry": _parse_mrl_expiry(o.operator_MRL_Expiry)})
#     return out


# def _compute_interlocks(db, cal, now, resources, alias, stages, qualified, stage_resources):
#     """Fills resource.blocks and returns the list shown on the Shifts & Interlocks tab."""
#     items: List[Dict[str, Any]] = []
#     today = now.date()
#     horizon_end = now + timedelta(days=120)

#     def add(res: Resource, typ: str, sev: str, msg: str, start: Optional[datetime], end: Optional[datetime]):
#         items.append({"resource_id": res.id, "resource_name": res.name, "type": typ, "severity": sev,
#                       "message": msg, "start_time": _iso(start) if start else None,
#                       "end_time": _iso(end) if end else None})
#         if sev == "BLOCK" and start:
#             res.blocks.append((start, end, msg))

#     def targets_of(machine_id) -> List[Resource]:
#         return list({id(r): r for r in alias.get(_norm(machine_id), [])}.values())

#     # 1. open breakdowns (machine is down until mc_ready_date is entered)
#     for b in db.query(MachineBreakdownLog).all():
#         if (b.status or "").strip().lower() in ("resolved", "closed") or b.mc_ready_date:
#             end = _parse_dt(b.mc_ready_date)
#             if not end or end <= now:
#                 continue
#         else:
#             end = None
#         start = _parse_dt(b.breakdown_date, b.breakdown_time) or now
#         for r in targets_of(b.machine_id):
#             add(r, "BREAKDOWN", "BLOCK", f"Breakdown: {b.issue_description or 'machine down'}", min(start, now), end)

#     # 2. planned preventive maintenance
#     first_start = min((sh.start_min for sh in cal.shifts if sh.active), default=360)
#     for m in db.query(MaintenanceScheduleLog).filter(
#             MaintenanceScheduleLog.year.in_([today.year - 1, today.year, today.year + 1])).all():
#         for q in (1, 2, 3, 4):
#             if (getattr(m, f"q{q}_status") or "").strip().lower() in ("completed", "complete", "done", "closed"):
#                 continue
#             d = _parse_date(getattr(m, f"q{q}_planned_date"))
#             if not d:
#                 continue
#             for r in targets_of(m.machine_id):
#                 if d < today:
#                     add(r, "MAINTENANCE", "WARN", f"Preventive maintenance overdue since {d:%d %b %Y}", None, None)
#                 elif datetime(d.year, d.month, d.day) < horizon_end:
#                     s = datetime(d.year, d.month, d.day) + timedelta(minutes=first_start)
#                     add(r, "MAINTENANCE", "BLOCK", f"Planned maintenance on {d:%d %b %Y}", s,
#                         s + timedelta(hours=PM_DOWNTIME_HOURS))

#     # 3. spares + machine consumables (inventory_spares is per equipment)
#     for it in db.query(InventorySpares).all():
#         qty, thr = _num(it.qty_avl), _num(it.threshold_qty)
#         if qty is None:
#             continue
#         stype = "CONSUMABLE" if "consumable" in str(it.sparetype or "").lower() else "SPARE"
#         targets = targets_of(it.eqpt_no) if it.eqpt_no else \
#             [r for r in stage_resources.get(it.flow_step_id, []) if r.kind == "MACHINE"]
#         what = f"{it.part_number or ''} {it.decription or ''}".strip()
#         lead = _days(it.procurement_lead_time)
#         for r in targets:
#             if qty <= 0:
#                 end = now + timedelta(days=lead) if lead and lead > 0 else None
#                 msg = f"{stype.title()} out of stock: {what}" + (f" (procurement {lead:g} d)" if end else " (no ETA)")
#                 add(r, stype, "BLOCK", msg, now, end)
#             elif thr is not None and qty < thr:
#                 sev = "BLOCK" if BLOCK_WHEN_BELOW_THRESHOLD else "WARN"
#                 add(r, stype, sev, f"{stype.title()} below threshold ({qty:g} < {thr:g}): {what}", now, None)

#     # 4. stage consumables (e.g. solder paste): expired / empty lots stop the whole stage
#     lots: Dict[Tuple[int, str], List[Any]] = defaultdict(list)
#     for c in db.query(Consumables).all():
#         if c.is_consumable_present is False or c.stage_id is None:
#             continue
#         lots[(c.stage_id, c.consumable_material_number or c.consumable_material_name or "?")].append(c)
#     for (sid, mat), rows in lots.items():
#         usable = [c for c in rows if (c.quantity or 0) > 0 and (not c.consumable_shelf_life or c.consumable_shelf_life >= today)]
#         name = rows[0].consumable_material_name or mat
#         targets = [r for r in stage_resources.get(sid, []) if r.kind == "MACHINE"]
#         if not usable:
#             for r in targets:
#                 add(r, "CONSUMABLE", "BLOCK", f"No usable stock of {name} (empty / shelf life expired)", now, None)
#         else:
#             dated = [c.consumable_shelf_life for c in usable if c.consumable_shelf_life]
#             if dated and len(dated) == len(usable) and max(dated) <= today + timedelta(days=CONSUMABLE_WARN_DAYS):
#                 for r in targets:
#                     add(r, "CONSUMABLE", "WARN", f"Last usable lot of {name} expires on {max(dated):%d %b %Y}", None, None)

#     # 5. operator qualification (MRL) - the scheduler also refuses expired operators
#     for sid, lst in qualified.items():
#         for q in lst:
#             if not q["expiry"]:
#                 continue
#             res = next((r for r in alias.get(_norm(q["staff"] or q["name"]), []) if r.kind == "MANUAL"), None)
#             label = stages[sid].step_name if sid in stages else sid
#             exp_day = (q["expiry"] - timedelta(days=1)).date()
#             holder = res or Resource(id=f"OP-{q['staff'] or q['name']}", name=q["name"], kind="MANUAL",
#                                      work_centre="", stage_id=sid, win_fn=lambda d: [])
#             if exp_day < today:
#                 items.append({"resource_id": holder.id, "resource_name": holder.name, "type": "OPERATOR_MRL",
#                               "severity": "BLOCK", "message": f"Qualification for '{label}' expired on {exp_day:%d %b %Y}",
#                               "start_time": None, "end_time": None})
#             elif (exp_day - today).days <= MRL_WARN_DAYS:
#                 items.append({"resource_id": holder.id, "resource_name": holder.name, "type": "OPERATOR_MRL",
#                               "severity": "WARN", "message": f"Qualification for '{label}' expires on {exp_day:%d %b %Y}",
#                               "start_time": None, "end_time": None})
#     items.sort(key=lambda i: (i["severity"] != "BLOCK", i["resource_name"]))
#     return items


# # ---------------------------------------- the schedule ------------------------------------
# def build_schedule(db: Session, time_source: str = "actual", history_days: int = 7,
#                    plant: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
#     now = (now or datetime.now()).replace(microsecond=0)
#     today = now.date()
#     warnings: List[str] = []

#     # ---- calendar: shifts (+ runtime changes), holidays
#     caps = db.query(StageCapacityDB).all()
#     shifts, derived = _load_shifts(db, caps)
#     changes = _load_changes(db)
#     op_shifts: Dict[str, List[str]] = defaultdict(list)
#     for a in db.query(OperatorShiftAssignment).all():
#         if _norm_staff(a.operator_staff_no):
#             op_shifts[_norm_staff(a.operator_staff_no)].append(a.shift_name)
#     public_hols = db.query(PublicHoliday).order_by(PublicHoliday.holiday_date).all()
#     exc_rows = db.query(CalendarException).order_by(CalendarException.exception_date).all()
#     cal = Calendar(public_holidays=[h.holiday_date for h in public_hols],
#                    exceptions={e.exception_date: e.exception_type for e in exc_rows},
#                    shifts=shifts, changes=changes, operator_shifts=op_shifts)
#     calendar_json = _calendar_json(cal, public_hols, exc_rows, today)

#     # ---- master data
#     stages = {s.flow_step_id: s for s in db.query(ProcessFlowMaster).all()}
#     batch_by_stage = {p.stage_id: bool(p.process_type_batch) for p in db.query(Processtype).all()}
#     cfg = _stage_cfg(db)
#     qualified = _qualified(db)
#     cap_by_name = {(c.name or "").strip().lower(): c for c in caps}

#     resources, stage_resources, stage_mode, alias, w = _build_resources(db, cal, stages, cfg, qualified, today)
#     warnings += w
#     interlocks = _compute_interlocks(db, cal, now, resources, alias, stages, qualified, stage_resources)
#     by_id = {r.id: r for r in resources}

#     # per stage: machine alias -> resource id (machine_id / eqpt_no only - machine names repeat)
#     stage_alias: Dict[int, Dict[str, str]] = {}
#     for sid, lst in stage_resources.items():
#         stage_alias[sid] = {_norm(a): r.id for r in lst if r.kind == "MACHINE"
#                             for a in (r.id, r.meta.get("eqpt_no")) if a}

#     # ---- production orders, quantities, progress
#     pcb_rows = db.query(PcbData.PCBserialNoPartNumber, PcbData.productionOrder, PcbData.partNumber,
#                         PcbData.quantity, PcbData.createdAt, PcbData.description
#                         ).filter(PcbData.productionOrder.isnot(None), PcbData.productionOrder != "").all()
#     po_pcbs: Dict[str, list] = defaultdict(list)
#     pcb_weight: Dict[str, tuple] = {}
#     for r in pcb_rows:
#         wgt = r.quantity if (r.quantity and r.quantity > 0) else 1
#         po_pcbs[r.productionOrder].append(r)
#         pcb_weight[r.PCBserialNoPartNumber] = (r.productionOrder, wgt)
#     tracking = {t.po_number: t for t in db.query(ProductionOrderTracking).all()}

#     def _empty_prog():
#         return {"done": 0, "started": 0, "start": None, "end": None,
#                 "ops": Counter(), "staff": Counter(), "machines": Counter()}

#     prog: Dict[tuple, Dict[str, Any]] = {}
#     serial_rows: List[Dict[str, Any]] = []          # one row per serial + stage: who worked on it and when
#     stale_seen: Counter = Counter()
#     log_rows = db.query(OperatorLog.PCBserialNoPartNumber, OperatorLog.current_step_id, OperatorLog.Task_Status,
#                         OperatorLog.start_time, OperatorLog.end_time, OperatorLog.operator_name,
#                         OperatorLog.operator_staff_no, OperatorLog.log_Data
#                         ).join(PcbData, PcbData.PCBserialNoPartNumber == OperatorLog.PCBserialNoPartNumber
#                                ).filter(PcbData.productionOrder.isnot(None)).all()
#     for lg in log_rows:
#         po_w = pcb_weight.get(lg.PCBserialNoPartNumber)
#         if not po_w:
#             continue
#         po_no, wgt = po_w
#         st_t, en_t = _as_log_time(lg.start_time), _as_log_time(lg.end_time)
#         p = prog.setdefault((po_no, lg.current_step_id), _empty_prog())
#         status = (lg.Task_Status or "").strip().upper().replace("-", "_")
#         if status in COMPLETED_STATUSES:
#             p["done"] += wgt
#             if en_t and (p["end"] is None or en_t > p["end"]):
#                 p["end"] = en_t
#         else:
#             p["started"] += wgt
#         if st_t and (p["start"] is None or st_t < p["start"]):
#             p["start"] = st_t
#         if lg.operator_name:
#             p["ops"][lg.operator_name] += 1
#         if lg.operator_staff_no:
#             p["staff"][_staff_key(lg.operator_staff_no)] += 1
#         m = _machine_from_log_data(lg.log_Data, stage_alias.get(lg.current_step_id))
#         if m:
#             p["machines"][m] += 1
#         # time this serial really occupied the operator: start -> completion / pause / now (if still running)
#         if st_t:
#             last_action = en_t or st_t
#             running = False
#             if status in COMPLETED_STATUSES or status not in RUNNING_STATUSES:
#                 fin = en_t                                     # completed, paused, rework raised ...
#             elif now - last_action <= timedelta(hours=STALE_RUNNING_HOURS):
#                 fin, running = now, True                       # being worked on right now
#             else:
#                 fin = en_t                                     # forgotten "Started" scan: does not occupy now
#                 stale_seen[po_no] += 1
#             if fin and fin > st_t:
#                 serial_rows.append({"po": po_no, "sid": lg.current_step_id, "start": st_t, "end": fin,
#                                     "staff": _staff_key(lg.operator_staff_no) if lg.operator_staff_no else None,
#                                     "running": running, "done": status in COMPLETED_STATUSES})
#     for po_no, n in stale_seen.items():
#         warnings.append(f"PO {po_no}: {n} serial(s) still 'Started' with no action for more than "
#                         f"{STALE_RUNNING_HOURS:g} h - not counted as running now")

#     # MANUAL stages: the operator is the resource and does one serial at a time. Walk each operator's serials
#     # in completion order and credit each serial only the time after the operator's previous completion
#     # (overlapping scans no longer stack up). MACHINE stages: one operator may tend several machines, so this
#     # is done per PO + stage only. Then consecutive serials of the same PO + stage are joined into one bar.
#     history: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
#     by_worker: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
#     for r in serial_rows:
#         manual = stage_mode.get(r["sid"]) == "MANUAL" and r["staff"]
#         by_worker[("OP", r["staff"]) if manual else ("PO", r["po"], r["sid"])].append(r)
#     gap = timedelta(minutes=HISTORY_MERGE_GAP_MINUTES)
#     for worker, rows in by_worker.items():
#         rows.sort(key=lambda r: (r["end"], r["start"]))
#         last_end, cur_blk = datetime.min, None
#         for r in rows:
#             a, b = max(r["start"], last_end), r["end"]
#             last_end = max(last_end, b)
#             if b <= a:
#                 if cur_blk and (cur_blk["po"], cur_blk["sid"]) == (r["po"], r["sid"]):
#                     cur_blk["serials"] += 1                    # finished inside the same stretch of work
#                     cur_blk["running"] |= r["running"]
#                     cur_blk["all_done"] &= r["done"]
#                 continue
#             if cur_blk and (cur_blk["po"], cur_blk["sid"]) == (r["po"], r["sid"]) and a - cur_blk["end"] <= gap:
#                 cur_blk["end"] = b
#                 cur_blk["serials"] += 1
#                 cur_blk["running"] |= r["running"]
#                 cur_blk["all_done"] &= r["done"]
#                 continue
#             cur_blk = {"po": r["po"], "sid": r["sid"], "staff": r["staff"], "start": a, "end": b, "serials": 1,
#                        "running": r["running"], "all_done": r["done"]}
#             history[(r["po"], r["sid"])].append(cur_blk)

#     # SPC entries also tell which machine a serial / PO ran on
#     serial_to_po: Dict[str, str] = {}
#     for r in pcb_rows:
#         serial_to_po[_norm(r.PCBserialNoPartNumber)] = r.productionOrder
#         serial_to_po[_norm(str(r.PCBserialNoPartNumber).split("$")[0])] = r.productionOrder
#     spc_machine: Dict[Tuple[str, int], str] = {}
#     for s in db.query(SPCDataLog.referral_number, SPCDataLog.stage_id, SPCDataLog.machine_id).all():
#         po = serial_to_po.get(_norm(s.referral_number)) or (s.referral_number if s.referral_number in po_pcbs else None)
#         rid = stage_alias.get(s.stage_id, {}).get(_norm(s.machine_id))
#         if po and rid:
#             spc_machine[(po, s.stage_id)] = rid

#     po_keys = sorted(set(po_pcbs) | set(tracking))
#     po_part: Dict[str, str] = {}
#     for po_no in po_keys:
#         parts = Counter(r.partNumber for r in po_pcbs.get(po_no, []) if r.partNumber)
#         part = parts.most_common(1)[0][0] if parts else (tracking[po_no].part_number if po_no in tracking else None)
#         if part:
#             po_part[po_no] = part
#     part_numbers = set(po_part.values())
#     products = {p.part_number: p for p in db.query(ProductTypeMaster).filter(
#         ProductTypeMaster.part_number.in_(part_numbers)).all()} if part_numbers else {}
#     routing: Dict[str, list] = defaultdict(list)
#     if part_numbers:
#         for r in db.query(ProductWorkflowMapping).filter(
#                 ProductWorkflowMapping.product_part_number.in_(part_numbers)
#         ).order_by(ProductWorkflowMapping.product_part_number, ProductWorkflowMapping.step_sequence).all():
#             routing[r.product_part_number].append(r)
#     times: Dict[tuple, list] = defaultdict(list)
#     if part_numbers:
#         for t in db.query(PartProcessTime).filter(PartProcessTime.part_no.in_(part_numbers)).all():
#             times[(t.part_no, t.process_id)].append(t)
#     other = "sap" if time_source == "actual" else "actual"

#     def hours_for(part: str, step_id: int, route_row=None):
#         """hours per PCB (series stage) or per batch (batch stage)"""
#         rows = times.get((part, step_id), [])
#         if rows:
#             plant_code = products[part].plant_code if part in products else None
#             row = next((r for r in rows if r.plant == plant_code), rows[0])
#             for src in (time_source, other):
#                 val = row.actual_time if src == "actual" else row.sap_time
#                 if val and val > 0:
#                     return float(val), src
#         if route_row is not None and getattr(route_row, "process_time_seconds", None):
#             return route_row.process_time_seconds / 3600.0, "routing"
#         stage = stages.get(step_id)
#         cap = cap_by_name.get((stage.step_name or "").strip().lower()) if stage else None
#         if cap and cap.manufacturing_time_mins and cap.manufacturing_time_mins > 0:
#             return float(cap.manufacturing_time_mins) / 60.0, "stage_capacity"
#         return None, "default"

#     orders: Dict[str, Dict[str, Any]] = {}
#     missing_time_seen: set = set()
#     for po_no in po_keys:
#         part = po_part.get(po_no)
#         trk = tracking.get(po_no)
#         if not part:
#             warnings.append(f"PO {po_no}: no part number")
#             continue
#         if plant and part in products and products[part].plant_code != plant:
#             continue
#         steps = routing.get(part, [])
#         if not steps:
#             warnings.append(f"PO {po_no}: part {part} has no routing in product_workflow_mapping")
#             continue
#         rows = po_pcbs.get(po_no, [])
#         pcb_qty = sum((r.quantity if (r.quantity and r.quantity > 0) else 1) for r in rows)
#         qty = max(pcb_qty, (trk.target_quantity if trk and trk.target_quantity else 0))
#         if qty <= 0:
#             warnings.append(f"PO {po_no}: quantity is 0")
#             continue
#         created = [r.createdAt for r in rows if r.createdAt]
#         release = min(created) if created else ((trk.started_at or trk.created_at) if trk else None) or now
#         description = (products[part].description if part in products and products[part].description
#                        else next((r.description for r in rows if r.description), "")) or ""
#         ops = []
#         for idx, st in enumerate(steps, start=1):
#             seq = st.step_sequence or idx
#             sid = st.flow_step_id
#             stage = stages.get(sid)
#             c = {**DEFAULT_CFG, **cfg.get(sid, {})}
#             mode = "BATCH" if batch_by_stage.get(sid) else "SERIES"
#             hrs, src = hours_for(part, sid, st)
#             if hrs is None and (part, sid) not in missing_time_seen:      # one warning per part + step, not per PO
#                 missing_time_seen.add((part, sid))
#                 warnings.append(f"Part {part}: no process time for '{stage.step_name if stage else sid}' "
#                                 f"(part_process_time / routing / stage_capacity) - 1 min assumed")
#             unit = hrs * 60.0 if hrs else 1.0                     # minutes per PCB (series) / per batch (batch)
#             p = prog.get((po_no, sid)) or _empty_prog()
#             ops.append({
#                 "op_id": f"{po_no}-{seq * 10}", "production_order": po_no, "part_number": part,
#                 "part_description": description, "operation_no": seq * 10, "flow_step_id": sid,
#                 "step_name": stage.step_name if stage else f"Step {sid}", "mode": mode, "quantity": qty,
#                 "unit_minutes": unit, "batch_size": min(int(c["batch_size"]), RULES["max_batch_size"]),
#                 "setup_minutes": c["setup_minutes"], "cooldown_minutes": c["cooldown_minutes"],
#                 "cooldown_working_only": c["cooldown_working_time_only"], "next_day_release": c["next_day_release"],
#                 "time_source_used": src, "done": min(p["done"], qty), "started": p["started"],
#                 "actual_start": p["start"], "actual_end": p["end"],
#                 "logged_operator": p["ops"].most_common(1)[0][0] if p["ops"] else "",
#                 "logged_staff": p["staff"], "logged_machines": p["machines"],
#             })
#         all_done = all(o["done"] >= qty for o in ops)
#         closed = bool(trk and (trk.status or "").strip().lower() in CLOSED_PO_STATUSES)
#         if all_done or closed:
#             ends = [o["actual_end"] for o in ops if o["actual_end"]]
#             if closed and trk:
#                 ends.append(trk.closed_at or trk.updated_at or release)
#             if (now - (max(ends) if ends else release)) > timedelta(days=history_days):
#                 continue
#         orders[po_no] = {"production_order": po_no, "part_number": part, "description": description,
#                          "quantity": qty, "release": release, "ops": ops}

#     # a routing stage that has no resource at all still needs one (should not happen after _build_resources)
#     for po in orders.values():
#         for op in po["ops"]:
#             if not stage_resources.get(op["flow_step_id"]):
#                 r = Resource(id=f"UNASSIGNED-{op['flow_step_id']}", name=f"{op['step_name']} (Unassigned)",
#                              kind="FALLBACK", work_centre=op["step_name"], stage_id=op["flow_step_id"],
#                              win_fn=cal.plant_fn(), meta={"stages": {op["flow_step_id"]}})
#                 resources.append(r)
#                 by_id[r.id] = r
#                 stage_resources[op["flow_step_id"]].append(r)

#     # operator name shown on machine operations (logged one, else rotate through the qualified pool)
#     for i, po in enumerate(orders.values()):
#         for op in po["ops"]:
#             pool = [q["name"] for q in qualified.get(op["flow_step_id"], []) if q["name"]]
#             op["display_operator"] = op["logged_operator"] or (pool[i % len(pool)] if pool else "")

#     # ---- which resource did started / finished work really use? (no extra table needed)
#     def actual_resource(op) -> Tuple[Optional[str], str]:
#         sid, cands = op["flow_step_id"], stage_resources.get(op["flow_step_id"], [])
#         if stage_mode.get(sid) == "MANUAL":                                   # 1. operator is the resource
#             by_staff = {_staff_key(r.operator): r.id for r in cands if r.kind == "MANUAL" and r.operator}
#             for staff, _ in op["logged_staff"].most_common():
#                 if staff in by_staff:
#                     return by_staff[staff], "operator_log"
#         if len(cands) == 1:                                                   # 2. only one resource at the stage
#             return cands[0].id, "single_resource"
#         if op["logged_machines"]:                                             # 3. machine recorded in the form
#             return op["logged_machines"].most_common(1)[0][0], "log_data"
#         rid = spc_machine.get((op["production_order"], sid))                  # 4. SPC entry
#         if rid:
#             return rid, "spc_log"
#         return None, "baseline_plan"                                          # 5. guess (old behaviour)

#     # ---- schedule: baseline plan, then current plan (history + running work fixed, manual moves pinned)
#     sequence = sorted(orders, key=lambda k: (orders[k]["release"], k))
#     plan = run_scheduler(cal, stage_resources, orders, sequence, rules=RULES)

#     fixed: Dict[str, Dict[str, Any]] = {}
#     for po in orders.values():
#         qty = po["quantity"]
#         for op in po["ops"]:
#             if not (op["done"] > 0 or op["started"] > 0):
#                 op["resource_source"] = "planner"
#                 continue
#             first = plan[op["op_id"]][0]
#             rid, src = actual_resource(op)
#             op["resource_source"] = src
#             res_id = rid or first["resource_id"]
#             if op["done"] >= qty:
#                 fixed[op["op_id"]] = {"resource_id": res_id, "confirmed": rid is not None,
#                                       "start": op["actual_start"] or first["start"],
#                                       "end": op["actual_end"] or max(t["end"] for t in plan[op["op_id"]])}
#             else:
#                 if op["mode"] == "BATCH":
#                     size = max(1, op["batch_size"])
#                     left = math.ceil((qty - op["done"]) / size) if op["done"] else math.ceil(qty / size)
#                     rem = left * op["unit_minutes"]
#                 else:
#                     rem = (qty - op["done"]) * op["unit_minutes"] if op["done"] else qty * op["unit_minutes"]
#                 fixed[op["op_id"]] = {"resource_id": res_id, "confirmed": rid is not None,
#                                       "start": op["actual_start"] or now, "remaining_minutes": max(rem, 1.0)}

#     pins: Dict[str, Dict[str, Any]] = {}
#     for tid, ov in {o.op_id: o for o in db.query(ScheduleOverride).all()}.items():
#         if ov.machine_id and ov.machine_id in by_id:
#             try:
#                 pins[tid] = {"resource_id": ov.machine_id,
#                              "start": datetime.fromisoformat(ov.start_time) if ov.start_time else None}
#             except ValueError:
#                 continue

#     cur = run_scheduler(cal, stage_resources, orders, sequence, fixed=fixed, pins=pins, not_before=now, rules=RULES)

#     # started / finished operations: draw what really happened serial by serial, then the queued remainder
#     for po in orders.values():
#         for op in po["ops"]:
#             blocks = history.get((op["production_order"], op["flow_step_id"]))
#             tasks = cur.get(op["op_id"], [])
#             if not blocks or len(tasks) != 1 or not tasks[0]["fixed"]:
#                 continue
#             t = tasks[0]
#             sid = op["flow_step_id"]
#             staff_res = {_staff_key(r.operator): r for r in stage_resources.get(sid, [])
#                          if r.kind == "MANUAL" and r.operator} if stage_mode.get(sid) == "MANUAL" else {}
#             new_list = []
#             for i, b in enumerate(sorted(blocks, key=lambda b: b["start"]), start=1):
#                 res = staff_res.get(b["staff"]) or by_id[t["resource_id"]]
#                 h = dict(t, task_id=f"{op['op_id']}-H{i}", resource_id=res.id, start=b["start"], end=b["end"],
#                          segments=[(b["start"], b["end"])], qty=b["serials"], batch_no=i,
#                          working_minutes=round((b["end"] - b["start"]).total_seconds() / 60.0, 1),
#                          history=True, running=b["running"], all_done=b["all_done"], new_segments=[])
#                 new_list.append(h)
#             new = t.get("new_segments") or []
#             if op["done"] < op["quantity"] and new:
#                 rem = dict(t, start=new[0][0], segments=new,
#                            working_minutes=round(sum((b - a).total_seconds() for a, b in new) / 60.0, 1),
#                            qty=op["quantity"] - op["done"], remaining=True)
#                 new_list.append(rem)
#             for x in new_list:
#                 x["batch_count"] = len(new_list)
#             cur[op["op_id"]] = new_list
#     notes = detect_conflicts(orders, cur, by_id)

#     # ---- JSON
#     plan_by_task = {t["task_id"]: t for ts in plan.values() for t in ts}
#     operations: List[Dict[str, Any]] = []
#     for po in orders.values():
#         qty = po["quantity"]
#         for op in po["ops"]:
#             pl_all = plan[op["op_id"]]
#             if op["done"] >= qty:
#                 op_status = "COMPLETED"
#             elif op["done"] > 0 or op["started"] > 0:
#                 op_status = "IN_PROGRESS"
#             else:
#                 op_status = "SCHEDULED"
#             for t in cur[op["op_id"]]:
#                 res = by_id[t["resource_id"]]
#                 if t.get("history"):
#                     entry_type = "HISTORY"
#                     status = "IN_PROGRESS" if t["running"] or not t["all_done"] else "COMPLETED"
#                 else:
#                     entry_type = "REMAINING" if t.get("remaining") or (t["fixed"] and op_status != "COMPLETED") \
#                         else "HISTORY" if t["fixed"] else "PLAN"
#                     status = op_status
#                 pt = plan_by_task.get(t["task_id"])
#                 if pt is None:                                    # running/finished op: show the whole planned span
#                     pt = {"resource_id": pl_all[0]["resource_id"], "start": min(x["start"] for x in pl_all),
#                           "end": max(x["end"] for x in pl_all),
#                           "segments": [s for x in pl_all for s in x["segments"]]}
#                 conflicts = list(notes.get(t["task_id"], []))
#                 if op["flow_step_id"] not in res.meta.get("stages", {res.stage_id}):
#                     conflicts.append(f"{res.name} is not set up for '{op['step_name']}'")
#                 operations.append({
#                     "op_id": t["task_id"], "parent_op_id": op["op_id"],
#                     "production_order": op["production_order"], "part_number": op["part_number"],
#                     "part_description": op["part_description"], "operation_no": op["operation_no"],
#                     "flow_step_id": op["flow_step_id"], "step_name": op["step_name"], "operation_name": op["step_name"],
#                     "process_mode": op["mode"], "quantity": qty, "completed_qty": op["done"],
#                     "serial_count": t["qty"] if t.get("history") else None,
#                     "batch_no": t["batch_no"], "batch_count": t["batch_count"], "batch_qty": t["qty"],
#                     "batch_size": op["batch_size"] if op["mode"] == "BATCH" else None,
#                     "duration_hours": round(t["working_minutes"] / 60.0, 2),
#                     "setup_minutes": t["setup_minutes"], "cooldown_minutes": op["cooldown_minutes"],
#                     "status": status, "op_status": op_status,
#                     "entry_type": entry_type,      # HISTORY (logged serials) | REMAINING (queued rest) | PLAN
#                     "operator_name": res.name if res.kind == "MANUAL" else op["display_operator"],
#                     "resource_type": res.kind,
#                     "resource_source": op.get("resource_source"),   # planner | operator_log | single_resource |
#                                                                     # log_data | spc_log | baseline_plan
#                     "machine_id": t["resource_id"], "start_time": _iso(t["start"]), "end_time": _iso(t["end"]),
#                     "segments": [[_iso(a), _iso(b)] for a, b in t["segments"]],
#                     "planned_machine_id": pt["resource_id"], "planned_start": _iso(pt["start"]),
#                     "planned_end": _iso(pt["end"]),
#                     "planned_segments": [[_iso(a), _iso(b)] for a, b in pt["segments"]],
#                     "time_source_used": op["time_source_used"], "waiting_reason": t["waiting_reason"],
#                     "interlock_blocked": t["blocked"], "interlock_reasons": t["block_reasons"],
#                     "pinned": t["pinned"], "conflicts": conflicts,
#                 })
#     operations.sort(key=lambda o: o["start_time"])

#     # ---- machine rows (resource_type tells the front end: MACHINE | MANUAL | FALLBACK)
#     def resource_state(r: Resource) -> str:
#         mine = [i for i in interlocks if i["resource_id"] == r.id]
#         active = [i for i in mine if i["severity"] == "BLOCK" and (i["start_time"] is None or i["start_time"] <= _iso(now))
#                   and (i["end_time"] is None or i["end_time"] > _iso(now))]
#         return "BLOCKED" if active else "WARN" if mine else "OK"

#     def order_key(r: Resource):
#         s = stages.get(r.stage_id)
#         return (s.step_order if s and s.step_order is not None else 10 ** 6, r.kind != "MACHINE", r.id)

#     machines = []
#     for r in sorted(resources, key=order_key):
#         machines.append({"machine_id": r.id, "work_centre": r.work_centre, "machine_name": r.name,
#                          "resource_type": r.kind, "stage_id": r.stage_id,
#                          "stage_ids": sorted(s for s in r.meta.get("stages", {r.stage_id}) if s is not None),
#                          "interlock_status": resource_state(r),
#                          "eqpt_no": r.meta.get("eqpt_no", ""), "eqpt_name": r.meta.get("eqpt_name", ""),
#                          "eqpt_make": r.meta.get("eqpt_make", "-"), "eqpt_model": r.meta.get("eqpt_model", "-")})

#     operators = []
#     seen_staff = set()
#     for sid, lst in qualified.items():
#         for q in lst:
#             if q["staff"] and q["staff"] not in seen_staff:
#                 seen_staff.add(q["staff"])
#                 operators.append({"staff_no": q["staff"], "name": q["name"], "shifts": op_shifts.get(q["staff"], [])})
#     operators.sort(key=lambda o: o["name"] or "")

#     return {
#         "generated_at": _iso(now), "calendar": calendar_json, "machines": machines,
#         "production_orders": [{"production_order": po["production_order"], "part_number": po["part_number"],
#                                "description": po["description"], "quantity": po["quantity"],
#                                "release_date": _iso(po["release"])}
#                               for po in sorted(orders.values(), key=lambda p: (p["release"], p["production_order"]))],
#         "operations": operations, "interlocks": interlocks, "operators": operators,
#         "shifts_derived": derived, "warnings": list(dict.fromkeys(warnings)),
#     }


# # ---------------------------------------- request bodies ----------------------------------
# class OpChange(BaseModel):
#     op_id: str
#     machine_id: Optional[str] = None
#     start_time: Optional[str] = None
#     end_time: Optional[str] = None


# class UpdatePayload(BaseModel):
#     changes: List[OpChange]


# class ConfigPayload(BaseModel):
#     stage_id: int
#     batch_size: int = 30
#     next_day_release: bool = False
#     cooldown_minutes: float = 0
#     cooldown_working_time_only: bool = False
#     setup_minutes: float = 0
#     resource_mode: str = "AUTO"
#     needs_operator: bool = False


# class ShiftPayload(BaseModel):
#     id: Optional[int] = None
#     name: str
#     start_time: str
#     end_time: str
#     is_active: bool = True
#     sort_order: int = 0


# class ShiftChangePayload(BaseModel):
#     date_from: date
#     date_to: Optional[date] = None
#     action: str                              # disable | enable | add
#     shift_name: Optional[str] = None
#     start_time: Optional[str] = None         # add: custom hours
#     end_time: Optional[str] = None
#     operator_staff_no: Optional[str] = None  # empty = whole plant
#     note: Optional[str] = None


# class OperatorShiftsPayload(BaseModel):
#     shifts: List[str]


# class ClearPayload(BaseModel):
#     op_ids: Optional[List[str]] = None


# # ---------------------------------------- endpoints ---------------------------------------
# def create_schedule_router(init_db, verify_token=None) -> APIRouter:
#     router = APIRouter(dependencies=[Depends(verify_token)] if verify_token else [])

#     @router.get("/schedule/data")
#     def get_schedule_data(time_source: str = "actual", history_days: int = 7, plant: Optional[str] = None,
#                           db: Session = Depends(init_db)):
#         """Everything the schedule tabs need in one call. The plan is recalculated on every call, so a
#         shift change / breakdown / stock update is visible as soon as the screen refreshes."""
#         if time_source not in ("actual", "sap"):
#             raise HTTPException(status_code=400, detail="time_source must be 'actual' or 'sap'")
#         if history_days < 0:
#             raise HTTPException(status_code=400, detail="history_days cannot be negative")
#         try:
#             return build_schedule(db, time_source=time_source, history_days=history_days, plant=plant)
#         except RuntimeError as e:
#             raise HTTPException(status_code=500, detail=str(e))

#     @router.post("/schedule/update")
#     def update_schedule(payload: UpdatePayload, db: Session = Depends(init_db)):
#         """Manual moves from the Machine Schedule 'Update' button (op_id = task id, e.g. 83086577-40-B2).
#         The task is pinned to that resource/start; later operations are re-planned around it."""
#         updated = 0
#         for ch in payload.changes:
#             for label, val in (("start_time", ch.start_time), ("end_time", ch.end_time)):
#                 if val:
#                     try:
#                         datetime.fromisoformat(val)
#                     except ValueError:
#                         raise HTTPException(status_code=400, detail=f"{ch.op_id}: {label} must be ISO YYYY-MM-DDTHH:MM:SS")
#             if not (ch.machine_id or ch.start_time or ch.end_time):
#                 continue
#             row = db.query(ScheduleOverride).filter(ScheduleOverride.op_id == ch.op_id).first()
#             if not row:
#                 row = ScheduleOverride(op_id=ch.op_id)
#                 db.add(row)
#             for f in ("machine_id", "start_time", "end_time"):
#                 v = getattr(ch, f)
#                 if v:
#                     setattr(row, f, v)
#             updated += 1
#         db.commit()
#         return {"updated": updated}

#     @router.post("/schedule/overrides/clear")
#     def clear_overrides(payload: ClearPayload, db: Session = Depends(init_db)):
#         q = db.query(ScheduleOverride)
#         if payload.op_ids:
#             q = q.filter(ScheduleOverride.op_id.in_(payload.op_ids))
#         n = q.delete(synchronize_session=False)
#         db.commit()
#         return {"cleared": n}

#     # ---------------- process config (batch size, setup, cooldown, resource mode) ----------------
#     @router.get("/schedule/config")
#     def get_config(db: Session = Depends(init_db)):
#         cfg = _stage_cfg(db)
#         batch = {p.stage_id: bool(p.process_type_batch) for p in db.query(Processtype).all()}
#         rows = []
#         for s in db.query(ProcessFlowMaster).order_by(ProcessFlowMaster.step_order, ProcessFlowMaster.flow_step_id).all():
#             c = {**DEFAULT_CFG, **cfg.get(s.flow_step_id, {})}
#             rows.append({"stage_id": s.flow_step_id, "stage_name": s.step_name,
#                          "process_mode": "BATCH" if batch.get(s.flow_step_id) else "SERIES",
#                          "is_default": s.flow_step_id not in cfg, **c})
#         return rows

#     @router.post("/schedule/config")
#     def save_config(p: ConfigPayload, db: Session = Depends(init_db)):
#         if not db.query(ProcessFlowMaster).filter(ProcessFlowMaster.flow_step_id == p.stage_id).first():
#             raise HTTPException(status_code=404, detail="Unknown stage")
#         if p.batch_size < 1 or p.batch_size > RULES["max_batch_size"]:
#             raise HTTPException(status_code=400, detail=f"batch size must be between 1 and {RULES['max_batch_size']}")
#         if p.cooldown_minutes < 0 or p.setup_minutes < 0:
#             raise HTTPException(status_code=400, detail="minutes cannot be negative")
#         mode = p.resource_mode.upper()
#         if mode not in ("AUTO", "MACHINE", "MANUAL"):
#             raise HTTPException(status_code=400, detail="resource_mode must be AUTO, MACHINE or MANUAL")
#         row = db.query(StageScheduleConfig).filter(StageScheduleConfig.stage_id == p.stage_id).first()
#         if not row:
#             row = StageScheduleConfig(stage_id=p.stage_id)
#             db.add(row)
#         row.batch_size, row.next_day_release = p.batch_size, p.next_day_release
#         row.cooldown_minutes, row.cooldown_working_time_only = p.cooldown_minutes, p.cooldown_working_time_only
#         row.setup_minutes, row.resource_mode, row.needs_operator = p.setup_minutes, mode, p.needs_operator
#         db.commit()
#         return {"saved": p.stage_id}

#     @router.delete("/schedule/config/{stage_id}")
#     def reset_config(stage_id: int, db: Session = Depends(init_db)):
#         db.query(StageScheduleConfig).filter(StageScheduleConfig.stage_id == stage_id).delete()
#         db.commit()
#         return {"reset": stage_id}

#     # ---------------- shifts (change them at runtime; the next refresh re-plans) ----------------
#     def _check_hm(v: Optional[str], label: str):
#         try:
#             _to_min(v)
#         except Exception:
#             raise HTTPException(status_code=400, detail=f"{label} must be HH:MM")

#     @router.get("/schedule/shifts")
#     def get_shifts(db: Session = Depends(init_db)):
#         shifts, derived = _load_shifts(db, db.query(StageCapacityDB).all())
#         rows = db.query(ShiftDefinition).order_by(ShiftDefinition.sort_order, ShiftDefinition.id).all()
#         by_name = {r.name: r for r in rows}
#         op_map: Dict[str, List[str]] = defaultdict(list)
#         for a in db.query(OperatorShiftAssignment).all():
#             op_map[a.operator_staff_no].append(a.shift_name)
#         return {
#             "derived": derived,
#             "shifts": [{"id": by_name[s.name].id if s.name in by_name else None, "name": s.name,
#                         "start_time": _hm(s.start_min), "end_time": _hm(s.end_min), "is_active": s.active}
#                        for s in shifts],
#             "changes": [{"id": c.id, "date_from": c.date_from.isoformat(),
#                          "date_to": (c.date_to or c.date_from).isoformat(), "action": c.action,
#                          "shift_name": c.shift_name, "start_time": c.start_time, "end_time": c.end_time,
#                          "operator_staff_no": c.operator_staff_no, "note": c.note}
#                         for c in db.query(ShiftAdjustment).order_by(ShiftAdjustment.date_from, ShiftAdjustment.id).all()],
#             "operator_shifts": dict(op_map),
#         }

#     @router.post("/schedule/shifts/seed")
#     def seed_shifts(db: Session = Depends(init_db)):
#         """Store the shifts derived from stage_capacity as editable shift_definition rows."""
#         if db.query(ShiftDefinition).count():
#             return {"seeded": 0}
#         shifts = _legacy_shifts(db.query(StageCapacityDB).all())
#         for i, s in enumerate(shifts):
#             db.add(ShiftDefinition(name=s.name, start_time=_hm(s.start_min), end_time=_hm(s.end_min),
#                                    is_active=True, sort_order=i))
#         db.commit()
#         return {"seeded": len(shifts)}

#     @router.post("/schedule/shifts")
#     def save_shift(p: ShiftPayload, db: Session = Depends(init_db)):
#         _check_hm(p.start_time, "start_time")
#         _check_hm(p.end_time, "end_time")
#         if _to_min(p.start_time) == _to_min(p.end_time):
#             raise HTTPException(status_code=400, detail="start and end cannot be the same")
#         row = db.query(ShiftDefinition).filter(ShiftDefinition.id == p.id).first() if p.id else None
#         if not row:
#             if db.query(ShiftDefinition).filter(ShiftDefinition.name == p.name).first():
#                 raise HTTPException(status_code=400, detail="A shift with this name already exists")
#             row = ShiftDefinition()
#             db.add(row)
#         row.name, row.start_time, row.end_time = p.name.strip(), p.start_time, p.end_time
#         row.is_active, row.sort_order = p.is_active, p.sort_order
#         db.commit()
#         return {"saved": row.id}

#     @router.delete("/schedule/shifts/{shift_id}")
#     def delete_shift(shift_id: int, db: Session = Depends(init_db)):
#         db.query(ShiftDefinition).filter(ShiftDefinition.id == shift_id).delete()
#         db.commit()
#         return {"deleted": shift_id}

#     @router.post("/schedule/shift-changes")
#     def add_shift_change(p: ShiftChangePayload, db: Session = Depends(init_db)):
#         action = p.action.lower()
#         if action not in ("disable", "enable", "add"):
#             raise HTTPException(status_code=400, detail="action must be disable, enable or add")
#         if p.date_to and p.date_to < p.date_from:
#             raise HTTPException(status_code=400, detail="date_to is before date_from")
#         if action == "add":
#             _check_hm(p.start_time, "start_time")
#             _check_hm(p.end_time, "end_time")
#         elif action == "enable" and not p.shift_name:
#             raise HTTPException(status_code=400, detail="choose the shift to enable")
#         db.add(ShiftAdjustment(date_from=p.date_from, date_to=p.date_to or p.date_from, action=action,
#                                shift_name=p.shift_name, start_time=p.start_time, end_time=p.end_time,
#                                operator_staff_no=_norm_staff(p.operator_staff_no), note=p.note))
#         db.commit()
#         return {"saved": True}

#     @router.delete("/schedule/shift-changes/{change_id}")
#     def delete_shift_change(change_id: int, db: Session = Depends(init_db)):
#         db.query(ShiftAdjustment).filter(ShiftAdjustment.id == change_id).delete()
#         db.commit()
#         return {"deleted": change_id}

#     @router.put("/schedule/operator-shifts/{staff_no}")
#     def set_operator_shifts(staff_no: str, p: OperatorShiftsPayload, db: Session = Depends(init_db)):
#         """Shifts one operator works (empty list = all active shifts)."""
#         db.query(OperatorShiftAssignment).filter(OperatorShiftAssignment.operator_staff_no == staff_no).delete()
#         for name in dict.fromkeys(p.shifts):
#             db.add(OperatorShiftAssignment(operator_staff_no=staff_no, shift_name=name))
#         db.commit()
#         return {"saved": staff_no, "shifts": p.shifts}

#     return router


# # ==========================================================================================
# # PART 3 - STANDALONE APP (port 2000)
# # ==========================================================================================
# SERVICE_PORT = int(os.getenv("SCHEDULE_SERVICE_PORT", "2000").strip() or 2000)


# def init_db():
#     db = SessionLocal()
#     try:
#         yield db
#     finally:
#         db.close()


# def _make_verify_token():
#     """Same rule as Data_Server: only checked when CHECK_JWT_VALIDATION == "True"."""
#     if os.getenv("CHECK_JWT_VALIDATION", "False") != "True":
#         return None
#     from jose import JWTError, jwt                  # only needed when validation is switched on
#     secret = os.getenv("JWT_SECRET")
#     algorithm = os.getenv("ALGORITHM", "HS256")
#     if not secret:
#         raise ValueError("CHECK_JWT_VALIDATION is True but JWT_SECRET is not set")

#     def verify_token(x_access_token: Optional[str] = Header(None, alias="x-access-token")):
#         if not x_access_token:
#             raise HTTPException(status_code=401, detail="Access Denied!")
#         try:
#             return jwt.decode(x_access_token, secret, algorithms=[algorithm])
#         except JWTError:
#             raise HTTPException(status_code=401, detail="Unauthorized! Token is invalid or expired.")
#     return verify_token


# app = FastAPI(title="SMT Schedule Service", version="1.0")
# app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
#                    allow_methods=["*"], allow_headers=["*"])

# try:
#     base.metadata.create_all(db_engine)            # creates only missing tables (shift_definition, ...)
# except Exception as exc:                           # never stop the service for this
#     print(f"[schedule-service] create_all skipped: {exc}")

# app.include_router(create_schedule_router(init_db, _make_verify_token()))


# @app.get("/schedule/health")
# def health():
#     return {"service": "smt-schedule", "status": "ok", "port": SERVICE_PORT,
#             "time": datetime.now().isoformat(timespec="seconds")}


# if __name__ == "__main__":
#     import uvicorn
#     print(f"[schedule-service] starting on port {SERVICE_PORT}")
#     uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
































"""
smt_schedule_service.py
=======================
Standalone SMT finite-capacity scheduling service, in ONE file:

    part 1  scheduling engine   (was smt_scheduler.py  - pure python, no database)
    part 2  database layer      (was smt_schedule_api.py - reads the MES tables, builds the plan)
    part 3  FastAPI app         (runs on its own, port 2000 by default)

Run
    python smt_schedule_service.py
    or:  uvicorn smt_schedule_service:app --host 0.0.0.0 --port 2000

Needs Database.py (session, engine) and Database_models.py in the same folder - the same two files
Data_Server.py uses. Python packages: fastapi, uvicorn, sqlalchemy, pydantic
(+ python-dotenv if you use a .env file, + python-jose if CHECK_JWT_VALIDATION is "True").

Environment (optional - read from .env when python-dotenv is installed)
    SCHEDULE_SERVICE_PORT       port, default 2000
    CHECK_JWT_VALIDATION        "True" -> every endpoint needs the x-access-token header (same as Data_Server)
    JWT_SECRET, ALGORITHM       used when CHECK_JWT_VALIDATION is "True"
    OPERATOR_LOG_SHIFT_MINUTES  minutes added to operator_log start/end times when they are read.
                                /operator/updatestatus writes them with datetime.utcnow(), so the default
                                "auto" adds this server's UTC offset (330 on an IST server). Set 0 if your
                                operator_log times are already local time.
    STALE_RUNNING_HOURS         a serial still "Started" with no action for longer than this is not treated as
                                occupying its operator / machine now (forgotten scans). Default 12.
    HISTORY_MERGE_GAP_MINUTES   consecutive serials of the same PO + stage on the same resource are drawn as one
                                bar when the gap between them is at most this. Default 30.
    HISTORY_MAX_FACTOR          a serial whose log lasts longer than this x the standard time (paused, rework, a
                                scan left open for days) is drawn as this x standard time ending at its completion.
                                Default 3; 0 = draw the full logged span.
    HISTORY_MIN_MINUTES         lower limit for that cap. Default 5.
    OPERATION_OVERLAP           started POs whose PCBs are spread over several stages:
                                "serial" (default) a PCB moves to the next stage as soon as it is done, so the next
                                stage may run alongside the previous one but always ends after it;
                                "none" PCBs not yet done by the previous stage wait until it has finished the PO.

Changes compared with the two original files
    1. Running / finished work is reserved on its resource BEFORE new work is planned, so new work is never
       placed on top of it. If the machine of running work is only a guess, it is moved to a free parallel
       machine of the same stage instead of piling up on one machine (cause of several serials / POs
       showing on the same machine at the same time in the Gantt).
    2. The resource actually used is taken from existing data where possible, in this order:
       operator_log operator (MANUAL stages) -> only one resource at the stage -> a machine field inside
       operator_log.log_Data -> spc_data_log. Otherwise the baseline plan's machine is used (old behaviour).
       Each operation in the response has "resource_source" telling which one was used.
    3. Operator MRL expiry stored as MMYY (e.g. "0127" or "127.0") is understood. It was silently ignored.
    4. The same machine mapped to several stages (one equipment_details row per stage) is ONE resource and
       can no longer be double-booked across stages. Set SHARE_MACHINE_ACROSS_STAGES = False for old behaviour.
    5. A machine (or an operator at a MANUAL stage) works on one serial at a time:
       - work already done is drawn from the real serial times in operator_log (one bar per run of consecutive
         serials, "entry_type": "HISTORY"), never as one long bar from the first scan of the PO to now.
         Overlapping logs on one resource are split: each serial is credited only the time after the previous
         completion on that resource. Logs far longer than the standard time are capped (HISTORY_MAX_FACTOR).
         If the machine of a started PO is not recorded, each serial goes to a machine of the stage that was free.
       - the remaining work of started POs is queued one after another on the operator / machine
         ("entry_type": "REMAINING") instead of all running in parallel from "now".
       - inside a started PO the stages follow the PCB flow: a stage works now only on PCBs the previous stage
         has finished ("remaining_kind": "AVAILABLE"); the rest waits for the previous stage
         ("remaining_kind": "WAITING", op_id ending in "-W") and always ends after it (OPERATION_OVERLAP).
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

try:                                    # .env support is optional
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ==========================================================================================
# PART 1 - SCHEDULING ENGINE (pure python)
# ==========================================================================================
"""
Rules implemented
* Routing is strictly sequential per production order (PO): operation k+1 starts only after
  EVERY piece of operation k has finished (plus cooldown / next-day release if configured).
* SERIES operation : one task for the whole PO quantity, time = qty x time-per-PCB.
* BATCH  operation : PO quantity is cut into batches of at most `batch_size` PCBs (max 30);
                     each batch is a separate task (time = time-per-batch) and the batches
                     are spread over the parallel machines / operators of that stage.
* MACHINE stages use machines, MANUAL stages use operators as the resource.
* Working time comes from SHIFTS (switchable / extendable per date range, per operator).
* Interlocks are time windows in which a resource cannot be used (breakdown, planned
  maintenance, spare / consumable out of stock, ...) and operator qualification expiry.
"""

Interval = Tuple[datetime, datetime]
WinFn = Callable[[date], List[Interval]]
FAR_FUTURE = datetime(2999, 1, 1)

DEFAULT_RULES: Dict[str, Any] = {
    "max_batch_size": 30,        # a batch can never hold more PCBs than this
    "default_batch_size": 30,
    "atomic_batches": True,      # a batch is not split over a shift break if it fits in one working piece
    "atomic_limit_minutes": 480,
    "horizon_days": 400,         # how far ahead a task may be searched
    # started POs, PCBs still to come from the previous stage:
    #   "serial": each PCB moves on as soon as it is done (next stage may run alongside, but ends after it)
    #   "none"  : they wait until the previous stage has finished the whole PO
    "operation_overlap": "serial",
}


def place_whole(win_fn: WinFn, blocks: List[Tuple[datetime, datetime]], start: datetime, minutes: float,
                horizon_days: int = 400) -> Tuple[List[Interval], datetime]:
    """like place(), but the job is never split around other jobs (only around non-working time):
    it fits a gap completely or goes after it - so bars on one resource never cross in the Gantt"""
    lb = start
    for _ in range(200):
        segs, e = place(win_fn, blocks, lb, minutes, 0, horizon_days)
        s0 = segs[0][0]
        inside = [b for a, b in blocks if a < e and b > s0]
        if not inside:
            return segs, e
        lb = max(inside)
    return segs, e


def _time_after(segs: List[Interval], minutes: float) -> Optional[datetime]:
    """moment at which `minutes` of work inside `segs` is done"""
    left = minutes
    for a, b in segs:
        m = (b - a).total_seconds() / 60.0
        if m >= left:
            return a + timedelta(minutes=left)
        left -= m
    return segs[-1][1] if segs else None


class NoSlot(Exception):
    """The task cannot be placed on this resource (blocked for ever / beyond the horizon)."""


# ---------------------------------------- interval helpers --------------------------------
def _merge_min(iv: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for s, e in sorted(iv):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def merge_dt(iv: List[Interval]) -> List[Interval]:
    out: List[Interval] = []
    for s, e in sorted(iv):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def union_fn(fns: List[WinFn]) -> WinFn:
    def f(d: date) -> List[Interval]:
        iv: List[Interval] = []
        for fn in fns:
            iv += fn(d)
        return merge_dt(iv)
    return f


def intersect_fn(fa: WinFn, fb: WinFn) -> WinFn:
    """Windows of A that are also inside windows of B (B is looked at +-1 day for night shifts)."""
    def f(d: date) -> List[Interval]:
        b: List[Interval] = []
        for k in (-1, 0, 1):
            b += fb(d + timedelta(days=k))
        b = merge_dt(b)
        out: List[Interval] = []
        for a0, a1 in fa(d):
            for b0, b1 in b:
                s, e = max(a0, b0), min(a1, b1)
                if s < e:
                    out.append((s, e))
        return merge_dt(out)
    return f


def iter_windows(win_fn: WinFn, start: datetime, horizon_days: int = 400) -> Iterator[Interval]:
    """Merged working intervals ending after `start`, in time order (first one is clipped to `start`)."""
    pending: Optional[Interval] = None
    first = start.date() - timedelta(days=1)       # a night shift of the day before may still be running
    for i in range(horizon_days + 2):
        for ws, we in win_fn(first + timedelta(days=i)):
            if we <= start:
                continue
            ws = max(ws, start)
            if pending and ws <= pending[1]:
                pending = (pending[0], max(pending[1], we))
            else:
                if pending:
                    yield pending
                pending = (ws, we)
    if pending:
        yield pending


def _free_pieces(ws: datetime, we: datetime, blocks: List[Tuple[datetime, datetime]]) -> Iterator[Interval]:
    cur = ws
    for bs, be in blocks:
        if be <= cur:
            continue
        if bs >= we:
            break
        if bs > cur:
            yield cur, bs
        cur = max(cur, be)
        if cur >= we:
            return
    if cur < we:
        yield cur, we


def place(win_fn: WinFn, blocks: List[Tuple[datetime, datetime]], start: datetime, minutes: float,
          atomic_limit: float = 0, horizon_days: int = 400) -> Tuple[List[Interval], datetime]:
    """
    Put `minutes` of work on a resource, starting no earlier than `start`, using only its working
    windows minus its blocked intervals.  Returns (working pieces, end).
    atomic_limit > 0 : if the job is not longer than this it must fit in ONE piece (no pause inside).
    """
    minutes = max(float(minutes), 1.0)
    blocks = sorted(blocks)
    dead_after = min((bs for bs, be in blocks if be >= FAR_FUTURE), default=None)   # blocked for ever from here
    atomic = 0 < minutes <= atomic_limit
    segs: List[Interval] = []
    remaining = minutes
    for ws, we in iter_windows(win_fn, start, horizon_days):
        if dead_after is not None and ws >= dead_after:
            break
        for ps, pe in _free_pieces(ws, we, blocks):
            length = (pe - ps).total_seconds() / 60.0
            if atomic and length + 1e-9 < remaining:
                continue                                  # too short for the whole job: wait for a longer piece
            use = min(length, remaining)
            segs.append((ps, ps + timedelta(minutes=use)))
            remaining -= use
            if remaining <= 1e-9:
                return segs, segs[-1][1]
    raise NoSlot()


def clip_to_windows(win_fn: WinFn, s: datetime, e: datetime) -> List[Interval]:
    """Working pieces of the span [s, e] (used for history that already happened)."""
    out = [(max(a, s), min(b, e)) for a, b in iter_windows(win_fn, s) if a < e]
    out = [(a, b) for a, b in out if a < b]
    return out or [(s, e)]


# ---------------------------------------- shifts + calendar -------------------------------
@dataclass
class ShiftDef:
    name: str
    start_min: int                 # minutes after midnight
    end_min: int                   # may be > 1440 for a night shift
    active: bool = True            # normally running?


@dataclass
class ShiftChange:
    """Runtime adjustment valid for date_from..date_to (inclusive)."""
    date_from: date
    date_to: date
    action: str                    # 'disable' | 'enable' | 'add'
    shift_name: Optional[str] = None   # disable/enable: which shift (None = every shift, 'disable' only)
    start_min: Optional[int] = None    # add: custom hours (extra shift / overtime)
    end_min: Optional[int] = None
    operator: Optional[str] = None     # None = whole plant, else operator staff no
    note: str = ""


class Calendar:
    def __init__(self, public_holidays=(), exceptions: Optional[Dict[date, str]] = None,
                 shifts: Optional[List[ShiftDef]] = None, changes: Optional[List[ShiftChange]] = None,
                 operator_shifts: Optional[Dict[str, List[str]]] = None, alt_saturday_off: bool = True):
        self.public_holidays = set(public_holidays)
        self.exceptions = {d: (t or "").strip().lower() for d, t in (exceptions or {}).items()}
        self.shifts = list(shifts or [ShiftDef("Shift 1", 360, 780), ShiftDef("Shift 2", 780, 1200)])
        self.changes = list(changes or [])
        self.operator_shifts = {k: set(v) for k, v in (operator_shifts or {}).items() if v}
        self.alt_saturday_off = alt_saturday_off
        self._cache: Dict[Tuple[date, Optional[str]], List[Tuple[int, int]]] = {}

    # 1 = full day, 0.5 = half day, 0 = off
    def day_factor(self, d: date) -> float:
        exc = self.exceptions.get(d)
        if exc == "working":
            return 1.0
        if exc == "half-day":
            return 0.5
        if exc == "holiday" or d in self.public_holidays or d.weekday() == 6:
            return 0.0
        if self.alt_saturday_off and d.weekday() == 5 and d.month >= 4 and ((d.day - 1) // 7 + 1) in (1, 3):
            return 0.0
        return 1.0

    def _minutes(self, d: date, operator: Optional[str]) -> List[Tuple[int, int]]:
        key = (d, operator)
        if key in self._cache:
            return self._cache[key]
        by_name = {s.name: s for s in self.shifts}
        factor = self.day_factor(d)
        active = {s.name for s in self.shifts if s.active} if factor > 0 else set()
        custom: List[Tuple[int, int]] = []

        def apply(who: Optional[str]):
            for ch in self.changes:
                if ch.operator != who or not (ch.date_from <= d <= ch.date_to):
                    continue
                if ch.action == "disable":
                    if ch.shift_name is None:
                        active.clear()
                        custom.clear()
                    else:
                        active.discard(ch.shift_name)
                elif ch.action == "enable" and ch.shift_name in by_name:
                    active.add(ch.shift_name)
                elif ch.action == "add" and ch.start_min is not None and ch.end_min is not None:
                    end = ch.end_min if ch.end_min > ch.start_min else ch.end_min + 1440
                    custom.append((ch.start_min, end))

        apply(None)
        if operator is not None:
            assigned = self.operator_shifts.get(operator)
            if assigned:
                active &= assigned
            apply(operator)

        iv = _merge_min([(by_name[n].start_min, by_name[n].end_min) for n in active] + custom)
        if factor == 0.5 and iv:                         # half day = first half of the working minutes
            keep = sum(e - s for s, e in iv) / 2.0
            trimmed = []
            for s, e in iv:
                take = min(e - s, keep)
                if take > 0:
                    trimmed.append((s, int(s + take)))
                keep -= take
            iv = trimmed
        self._cache[key] = iv
        return iv

    def windows(self, d: date, operator: Optional[str] = None) -> List[Interval]:
        base = datetime(d.year, d.month, d.day)
        return [(base + timedelta(minutes=s), base + timedelta(minutes=e)) for s, e in self._minutes(d, operator)]

    def plant_fn(self) -> WinFn:
        return lambda d: self.windows(d)

    def operator_fn(self, staff_no: str) -> WinFn:
        return lambda d: self.windows(d, staff_no)

    def next_working_start(self, dt: datetime) -> datetime:
        for ws, _ in iter_windows(self.plant_fn(), dt):
            return ws
        raise RuntimeError("No working time found - check shifts / holidays")

    def add_working_minutes(self, start: datetime, minutes: float) -> datetime:
        return place(self.plant_fn(), [], start, minutes)[1]

    def day_windows_json(self, d_from: date, d_to: date) -> Tuple[Dict[str, List[List[int]]], float, float]:
        """Working windows per calendar day (minutes after midnight, night shifts cut at midnight) for the
        front-end, plus the hull (first start hour, last end hour) of everything in the range."""
        out: Dict[str, List[List[int]]] = {}
        lo, hi, n = 1440, 0, (d_to - d_from).days
        fn = self.plant_fn()
        for i in range(-1, n + 2):
            d = d_from + timedelta(days=i)
            for ws, we in fn(d):
                cur = ws
                while cur < we:
                    day_end = datetime(cur.year, cur.month, cur.day) + timedelta(days=1)
                    piece_end = min(we, day_end)
                    day = cur.date()
                    if d_from <= day <= d_to:
                        s = cur.hour * 60 + cur.minute
                        e = 1440 if piece_end == day_end else piece_end.hour * 60 + piece_end.minute
                        out.setdefault(day.isoformat(), []).append([s, e])
                        lo, hi = min(lo, s), max(hi, e)
                    cur = piece_end
        for k in out:
            out[k] = [list(x) for x in _merge_min([tuple(x) for x in out[k]])]
        if hi == 0:
            return out, 6.0, 20.0
        return out, math.floor(lo / 60.0), math.ceil(hi / 60.0)


# ---------------------------------------- resources ---------------------------------------
@dataclass
class Resource:
    id: str
    name: str
    kind: str                                   # MACHINE | MANUAL | FALLBACK
    work_centre: str
    stage_id: Optional[int]
    win_fn: WinFn
    blocks: List[Tuple[datetime, Optional[datetime], str]] = field(default_factory=list)  # (from, to|None, reason)
    operator: Optional[str] = None              # staff no (MANUAL)
    valid_until: Dict[int, datetime] = field(default_factory=dict)   # stage_id -> qualification expiry
    meta: Dict[str, Any] = field(default_factory=dict)

    def hard_blocks(self) -> List[Tuple[datetime, datetime]]:
        return sorted((s, e or FAR_FUTURE) for s, e, _ in self.blocks)

    def reasons_near(self, s: datetime, e: datetime) -> List[str]:
        out = [r for bs, be, r in self.blocks if bs < e and (be or FAR_FUTURE) > s]
        return out or [r for bs, be, r in self.blocks if be is None]


# ---------------------------------------- scheduler ---------------------------------------
def _release_after(cal: Calendar, op: Dict[str, Any], end: datetime) -> datetime:
    """Earliest moment the NEXT operation of the order may start (cooldown, next-day release)."""
    ready = end
    cd = float(op.get("cooldown_minutes") or 0)
    if cd > 0:
        ready = cal.add_working_minutes(end, cd) if op.get("cooldown_working_only") else end + timedelta(minutes=cd)
    if op["mode"] == "BATCH" and op.get("next_day_release"):
        ready = max(ready, datetime.combine(end.date() + timedelta(days=1), time(0)))
    return ready


def batch_split(op: Dict[str, Any], rules: Dict[str, Any]) -> List[Tuple[int, float]]:
    """[(pcbs, minutes)] for every task of the operation."""
    qty = int(op["quantity"])
    if op["mode"] == "BATCH":
        size = int(op.get("batch_size") or rules["default_batch_size"])
        size = max(1, min(size, rules["max_batch_size"]))
        n = max(1, math.ceil(qty / size))
        return [(min(size, qty - size * k), float(op["unit_minutes"])) for k in range(n)]
    return [(qty, max(qty * float(op["unit_minutes"]), 1.0))]


def run_scheduler(cal: Calendar, stage_resources: Dict[int, List[Resource]], orders: Dict[str, Dict[str, Any]],
                  sequence: List[str], fixed: Optional[Dict[str, Dict[str, Any]]] = None,
                  pins: Optional[Dict[str, Dict[str, Any]]] = None, not_before: Optional[datetime] = None,
                  rules: Optional[Dict[str, Any]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    orders[po] = {"release": datetime, "part_number": str, "ops": [op, ...]} ; op keys:
        op_id, flow_step_id, mode ('SERIES'|'BATCH'), quantity, unit_minutes (per PCB / per batch),
        batch_size, setup_minutes, cooldown_minutes, cooldown_working_only, next_day_release
    fixed[op_id] : work that already happened / is running
                   {resource_id, start, end}                (finished)
                   {resource_id, start, remaining_minutes}  (running)
                   optional "confirmed": True when resource_id is the machine really used
    pins[task_id]: manual move of one task                   {resource_id, start}
    returns {op_id: [task, ...]}
    """
    rules = {**DEFAULT_RULES, **(rules or {})}
    by_id = {r.id: r for lst in stage_resources.values() for r in lst}
    state: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"free": datetime.min, "last_part": None})
    result: Dict[str, List[Dict[str, Any]]] = {}
    horizon = rules["horizon_days"]
    nb = not_before or datetime.min

    # 1. Manual moves (pins) are reserved first: they keep their slot and everything else is planned around them.
    reserved: Dict[str, List[Tuple[datetime, datetime]]] = defaultdict(list)
    pin_slots: Dict[str, Tuple[Resource, List[Interval], datetime]] = {}
    for po_no in sequence:
        for op in orders[po_no]["ops"]:
            if (fixed and op["op_id"] in fixed) or not pins:
                continue
            pieces = batch_split(op, rules)
            for k, (_, minutes) in enumerate(pieces, start=1):
                tid = op["op_id"] if len(pieces) == 1 else f"{op['op_id']}-B{k}"
                pin = pins.get(tid)
                if pin and pin.get("start") and pin.get("resource_id") in by_id:
                    res = by_id[pin["resource_id"]]
                    atomic = rules["atomic_limit_minutes"] if (op["mode"] == "BATCH" and rules["atomic_batches"]) else 0
                    try:
                        segs, end = place(res.win_fn, [], pin["start"], minutes, atomic, horizon)
                    except NoSlot:
                        continue
                    pin_slots[tid] = (res, segs, end)
                    reserved[res.id] += segs

    # 2. FIX: running / finished work is reserved BEFORE new work is planned, so new work goes around it.
    #    A resource does one job at a time: the remaining work of several started POs on the same operator /
    #    machine is queued one after the other (the PO started first goes first), not run in parallel.
    #    Within a PO the stages follow the PCB flow: a stage can only work on PCBs the previous stage has
    #    already finished; the rest waits for the previous stage and the stage always ends after it.
    #    If the resource of running work is only a guess, the stage resource that finishes it first is used.
    fixed_slots: Dict[str, Tuple[Resource, datetime, datetime, List[Interval], List[Interval]]] = {}
    fixed_parts: Dict[str, List[Dict[str, Any]]] = {}
    if fixed:
        po_first = {}
        finished, running = [], []
        for po_no in sequence:
            ops = orders[po_no]["ops"]
            starts = [fixed[o["op_id"]]["start"] for o in ops if o["op_id"] in fixed]
            if starts:
                po_first[po_no] = min(starts)
            for idx, op in enumerate(ops):
                if op["op_id"] in fixed:
                    (running if "remaining_minutes" in fixed[op["op_id"]] else finished).append((po_no, idx, op))
        running.sort(key=lambda x: (po_first[x[0]], x[1]))
        for po_no, idx, op in finished + running:
            fx = fixed[op["op_id"]]
            cands = stage_resources[op["flow_step_id"]]
            res = by_id.get(fx["resource_id"]) or cands[0]
            s = fx["start"]
            new: List[Interval] = []
            if "remaining_minutes" in fx:
                from_t = max(nb, s)
                mins = fx["remaining_minutes"]
                # ---- PCB flow from the previous stage of the same PO (only when that stage is still running)
                avail_min, wait_min, up_end, up_first, up_name = mins, 0.0, None, None, ""
                prev = orders[po_no]["ops"][idx - 1] if idx > 0 else None
                if prev is not None and prev["op_id"] in fixed_slots and "remaining_minutes" in fixed[prev["op_id"]]:
                    qty = op.get("quantity") or 1
                    rem_q = max(qty - op.get("done", 0), 0)
                    avail_q = min(rem_q, max(0.0, min(qty, prev.get("done", 0)) - op.get("done", 0)))
                    if rem_q > 0 and avail_q < rem_q:
                        avail_min = mins * avail_q / rem_q
                        wait_min = mins - avail_min
                        up = fixed_slots[prev["op_id"]]
                        up_end = up[2]
                        up_first = _time_after(up[4], max(prev.get("unit_minutes", 1.0), 1.0)) or up_end
                        up_name = prev.get("step_name", "previous stage")
                        if rules["operation_overlap"] == "none":
                            up_first = up_end
                unit_k = max(op.get("unit_minutes", 1.0), 1.0)

                def plan_on(r: Resource):
                    blocks = list(reserved.get(r.id, []))
                    parts: List[Dict[str, Any]] = []
                    end = from_t
                    try:
                        if avail_min > 1e-6:                  # PCBs already here: start as soon as possible
                            segs_a, end = place_whole(r.win_fn, blocks, from_t, avail_min, horizon)
                            blocks += segs_a
                            parts.append({"kind": "AVAILABLE", "segments": segs_a, "share": avail_min / mins})
                        if wait_min > 1e-6:                   # PCBs still to come: one block that starts after
                            last = min(wait_min, unit_k)      # the first PCB arrives and ends after the last one
                            target = place(r.win_fn, blocks, up_end, last, 0, horizon)[1]
                            cand = max(end, up_first or from_t)
                            for _ in range(30):
                                segs_w, end = place_whole(r.win_fn, blocks, cand, wait_min, horizon)
                                if end >= target - timedelta(seconds=1):
                                    break
                                cand += target - end
                            parts.append({"kind": "WAITING", "segments": segs_w, "share": wait_min / mins,
                                          "reason": f"waiting for PCBs from '{up_name}'"})
                    except NoSlot:
                        return None
                    return parts, end

                options = [res] if fx.get("confirmed") else [res] + [r for r in cands if r is not res]
                best = None
                for r in options:
                    got = plan_on(r)
                    if got and (best is None or got[1] < best[1][1]):
                        best = (r, got)
                if best:
                    res, (parts, e) = best
                else:                                     # resource has no working time at all: show it anyway
                    parts = [{"kind": "AVAILABLE", "share": 1.0,
                              "segments": [(from_t, from_t + timedelta(minutes=max(mins, 1.0)))]}]
                    e = parts[0]["segments"][-1][1]
                new = sorted(x for pt in parts for x in pt["segments"])
                fixed_parts[op["op_id"]] = parts
                segs = (clip_to_windows(res.win_fn, s, from_t) if from_t > s else []) + new
            else:
                e = max(fx["end"], s)
                segs = clip_to_windows(res.win_fn, s, e) if e > s else [(s, e)]
            fixed_slots[op["op_id"]] = (res, s, e, segs, new)
            reserved[res.id] += [(a, b) for a, b in segs if b > nb]   # only the future part blocks the machine

    # 3. Everything else, PO by PO in sequence order, operation by operation.
    for po_no in sequence:
        po = orders[po_no]
        part = po["part_number"]
        ready = po["release"]
        for op in po["ops"]:
            lower = ready if not_before is None else max(ready, not_before)
            tasks: List[Dict[str, Any]]

            if op["op_id"] in fixed_slots:                                 # history / running work
                res, s, e, segs, new = fixed_slots[op["op_id"]]
                tasks = [_task(op, 1, 1, op["quantity"], res, s, e, segs, 0.0, status_fixed=True)]
                tasks[0]["new_segments"] = new
                tasks[0]["remaining_parts"] = fixed_parts.get(op["op_id"], [])
                state[res.id]["free"] = max(state[res.id]["free"], e)
                state[res.id]["last_part"] = part
            else:
                tasks = _schedule_op(cal, op, part, lower, stage_resources, by_id, state, pins or {}, rules,
                                     reserved, pin_slots)

            result[op["op_id"]] = tasks
            ready = _release_after(cal, op, max(t["end"] for t in tasks))
    return result


def _task(op, no, count, qty, res: Resource, start, end, segs, setup, status_fixed=False, pinned=False,
          blocked=False, reasons=None, waiting="") -> Dict[str, Any]:
    tid = op["op_id"] if count == 1 else f"{op['op_id']}-B{no}"
    work = sum((b - a).total_seconds() for a, b in segs) / 60.0
    return {"task_id": tid, "op_id": op["op_id"], "resource_id": res.id, "start": start, "end": end,
            "segments": segs, "qty": qty, "batch_no": no, "batch_count": count, "setup_minutes": round(setup, 1),
            "working_minutes": round(work, 1), "fixed": status_fixed, "pinned": pinned,
            "blocked": blocked, "block_reasons": reasons or [], "waiting_reason": waiting}


def _schedule_op(cal, op, part, lower, stage_resources, by_id, state, pins, rules, reserved,
                 pin_slots) -> List[Dict[str, Any]]:
    cands = stage_resources[op["flow_step_id"]]
    sid = op["flow_step_id"]
    pieces = batch_split(op, rules)
    count = len(pieces)
    atomic = rules["atomic_limit_minutes"] if (op["mode"] == "BATCH" and rules["atomic_batches"]) else 0
    horizon = rules["horizon_days"]
    tasks: List[Dict[str, Any]] = []

    def try_res(res: Resource, lb: datetime, minutes: float, honour_blocks: bool, honour_expiry: bool):
        st = state[res.id]
        start_lb = max(lb, st["free"])
        setup = float(op.get("setup_minutes") or 0) if st["last_part"] != part else 0.0
        try:
            blocks = (res.hard_blocks() if honour_blocks else []) + reserved.get(res.id, [])
            segs, end = place(res.win_fn, blocks, start_lb, minutes + setup, atomic, horizon)
        except NoSlot:
            return None
        exp = res.valid_until.get(sid)
        if honour_expiry and exp and segs[0][0] >= exp:
            return None
        return segs, end, setup

    for k, (qty, minutes) in enumerate(pieces, start=1):
        tid = op["op_id"] if count == 1 else f"{op['op_id']}-B{k}"
        pin = pins.get(tid)
        chosen = None                      # (resource, segments, end, setup)
        blocked = False
        pinned_slot = False

        if tid in pin_slots:                                  # supervisor moved this task: keep its reserved slot
            res, segs, end = pin_slots[tid]
            chosen = (res, segs, end, 0.0)
            pinned_slot = True
        elif pin and pin.get("resource_id") in by_id:        # machine chosen by hand, time left to the planner
            res = by_id[pin["resource_id"]]
            got = try_res(res, lower, minutes, False, False)
            if got:
                chosen = (res,) + got
        if chosen is None:
            # 1st try: respect every interlock; 2nd: ignore interlocks; 3rd: also ignore operator expiry.
            for honour_blocks, honour_expiry in ((True, True), (False, True), (False, False)):
                best = None
                for res in cands:
                    got = try_res(res, lower, minutes, honour_blocks, honour_expiry)
                    if got and (best is None or (got[1], got[0][0][0]) < (best[2], best[1][0][0])):
                        best = (res,) + got
                if best:
                    chosen, blocked = best, not (honour_blocks and honour_expiry)
                    break
        if chosen is None:
            raise RuntimeError(f"Operation {op['op_id']} cannot be placed inside the horizon")

        res, segs, end, setup = chosen
        start = segs[0][0]
        reasons: List[str] = []
        if blocked:
            reasons = res.reasons_near(start, end)
            if not honour_expiry_ok(res, sid, start):
                reasons.append("No operator with a valid qualification for this step")
        st = state[res.id]
        waiting = ""
        if start > lower + timedelta(minutes=1):
            waiting = (f"Waiting for {res.name} (busy until {st['free']:%d %b %H:%M})" if st["free"] > lower
                       else "Waiting for working time (shift / holiday / interlock)")
        if not pinned_slot:                                   # a reserved slot does not push the resource's free time
            st["free"] = max(st["free"], end)
        st["last_part"] = part
        tasks.append(_task(op, k, count, qty, res, start, end, segs, setup, pinned=bool(pin), blocked=blocked,
                           reasons=reasons, waiting=waiting))
    return tasks


def honour_expiry_ok(res: Resource, stage_id: int, start: datetime) -> bool:
    exp = res.valid_until.get(stage_id)
    return not (exp and start >= exp)


# ------------------------ post checks (red notes in the Gantt tooltip, never stop the plan) ------------
def detect_conflicts(orders: Dict[str, Dict[str, Any]], result: Dict[str, List[Dict[str, Any]]],
                     by_id: Dict[str, Resource]) -> Dict[str, List[str]]:
    notes: Dict[str, List[str]] = defaultdict(list)

    # 1. sequence: every task must start after the previous operation has finished
    for po in orders.values():
        prev_end, prev_name = None, ""
        for op in po["ops"]:
            tasks = result.get(op["op_id"], [])
            if prev_end is not None:
                for t in tasks:
                    if t.get("history") or t.get("flow_checked"):
                        continue                              # real serial flow / PCB flow already applied
                    if t["start"] + timedelta(minutes=1) < prev_end:
                        notes[t["task_id"]].append(f"Starts before '{prev_name}' of the same order has finished")
            if tasks:
                prev_end, prev_name = max(t["end"] for t in tasks), op["step_name"]

    # 2. one resource cannot do two things at once;  3. interlock windows
    per_res: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for tasks in result.values():
        for t in tasks:
            per_res[t["resource_id"]].append(t)
    for rid, lst in per_res.items():
        lst.sort(key=lambda t: t["start"])
        for a, b in zip(lst, lst[1:]):
            if b["start"] + timedelta(minutes=1) < a["end"] and any(
                    x[0] < y[1] and y[0] < x[1] for x in a["segments"] for y in b["segments"]):
                notes[a["task_id"]].append(f"Overlaps {b['task_id']} on the same resource")
                notes[b["task_id"]].append(f"Overlaps {a['task_id']} on the same resource")
        res = by_id.get(rid)
        if res:
            for t in lst:
                if t["blocked"] or t.get("history"):
                    continue                                  # provisional / already happened
                for bs, be, reason in res.blocks:
                    if any(s < (be or FAR_FUTURE) and bs < e for s, e in t["segments"]):
                        notes[t["task_id"]].append(f"Runs during interlock: {reason}")
    return notes


# ==========================================================================================
# PART 2 - DATABASE LAYER (reads the MES tables, builds the plan)
# ==========================================================================================
from Database import session as SessionLocal, engine as db_engine          # noqa: E402
from Database_models import (                                               # noqa: E402
    base, CalendarException, Consumables, Equipments, InventorySpares, MachineBreakdownLog,
    MaintenanceScheduleLog, OperatorLog, OperatorShiftAssignment, OperatorStepMapping, PartProcessTime,
    PcbData, ProcessFlowMaster, Processtype, ProductionOrderTracking, ProductTypeMaster,
    ProductWorkflowMapping, PublicHoliday, ScheduleOverride, ShiftAdjustment, ShiftDefinition,
    SPCDataLog, StageCapacityDB, StageScheduleConfig,
)

# ---------------------------------------- settings you may want to tune -------------------
ISO = "%Y-%m-%dT%H:%M:%S"
COMPLETED_STATUSES = {"COMPLETED", "COMPLETE", "DONE", "FINISHED"}
CLOSED_PO_STATUSES = {"completed", "closed", "cancelled", "canceled"}
RULES = {"max_batch_size": 30, "default_batch_size": 30,      # max PCBs per batch = 30
         "operation_overlap": (os.getenv("OPERATION_OVERLAP", "serial") or "serial").strip().lower()}
PM_DOWNTIME_HOURS = 8               # a planned preventive-maintenance day blocks the machine for this long
BLOCK_WHEN_BELOW_THRESHOLD = False  # False: spare/consumable blocks the machine only when qty_avl <= 0
MRL_WARN_DAYS = 30                  # warn when an operator qualification expires within this many days
CONSUMABLE_WARN_DAYS = 7            # warn when the last usable consumable lot expires within this many days
SHARE_MACHINE_ACROSS_STAGES = True  # one equipment mapped to several stages = one resource (no double booking)


def _log_shift_minutes() -> float:
    v = (os.getenv("OPERATOR_LOG_SHIFT_MINUTES", "auto") or "auto").strip().lower()
    if v == "auto":                                  # operator_log is written in UTC -> this server's local time
        off = datetime.now().astimezone().utcoffset()
        return off.total_seconds() / 60.0 if off else 0.0
    return float(v)


OPERATOR_LOG_SHIFT_MINUTES = _log_shift_minutes()
STALE_RUNNING_HOURS = float(os.getenv("STALE_RUNNING_HOURS", "12") or 12)
HISTORY_MERGE_GAP_MINUTES = float(os.getenv("HISTORY_MERGE_GAP_MINUTES", "30") or 30)
HISTORY_MAX_FACTOR = float(os.getenv("HISTORY_MAX_FACTOR", "3") or 0)
HISTORY_MIN_MINUTES = float(os.getenv("HISTORY_MIN_MINUTES", "5") or 5)
RUNNING_STATUSES = {"STARTED", "START", "REWORK_STARTED", "IN_PROGRESS", "RUNNING", "RESUMED"}
DEFAULT_CFG = {"batch_size": 30, "next_day_release": False, "cooldown_minutes": 0.0,
               "cooldown_working_time_only": False, "setup_minutes": 0.0, "resource_mode": "AUTO",
               "needs_operator": False}


# ---------------------------------------- small helpers -----------------------------------
def _iso(dt: datetime) -> str:
    return dt.strftime(ISO)


def _hm(minutes: float) -> str:
    m = int(round(minutes)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _to_min(hhmm: str) -> int:
    h, m = str(hhmm).strip().split(":")[:2]
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(hhmm)
    return h * 60 + m


def _num(v) -> Optional[float]:
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def _days(v) -> Optional[float]:
    n = _num(v)
    if n is None:
        return None
    t = str(v).lower()
    return n * 7 if "week" in t else n * 30 if "month" in t else n


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d.%m.%Y", "%d %b %Y", "%m/%d/%Y")


def _parse_date(v) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip().replace("Z", "")
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s[:11].strip() if "%b" in f else s[:10], f).date()
        except ValueError:
            continue
    return None


def _parse_dt(date_s, time_s=None) -> Optional[datetime]:
    if not date_s:
        return None
    if isinstance(date_s, datetime):
        return date_s
    try:
        return datetime.fromisoformat(str(date_s).replace("Z", "").strip())
    except ValueError:
        pass
    d = _parse_date(date_s)
    if not d:
        return None
    t = datetime.min
    for f in ("%H:%M:%S", "%H:%M", "%I:%M %p"):
        try:
            t = datetime.strptime(str(time_s or "00:00").strip(), f)
            break
        except ValueError:
            continue
    return datetime(d.year, d.month, d.day, t.hour, t.minute)


def _parse_mrl_expiry(v) -> Optional[datetime]:
    """
    FIX: operator_MRL_Expiry is stored as MMYY ("0127", or "127.0" after an Excel import). The rest of the
    app reads it with strptime("%m%y"), i.e. the qualification is valid until the 1st of that month.
    Full dates are still accepted (valid to the end of that day).
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit() and len(s) in (3, 4):
        try:
            return datetime.strptime(s.zfill(4), "%m%y")
        except ValueError:
            return None
    d = _parse_date(s)
    return datetime(d.year, d.month, d.day) + timedelta(days=1) if d else None


def _as_log_time(v) -> Optional[datetime]:
    """operator_log time -> local datetime (applies OPERATOR_LOG_SHIFT_MINUTES)."""
    dt = v if isinstance(v, datetime) else _parse_dt(v)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt + timedelta(minutes=OPERATOR_LOG_SHIFT_MINUTES)


def _norm(s) -> str:
    s = str(s or "").strip().lower()
    return s[:-2] if s.endswith(".0") else s


def _norm_staff(v) -> Optional[str]:
    v = (str(v).strip() if v is not None else "")
    return v or None


def _staff_key(s) -> str:
    """'E0169', 'e169', '169' -> '169' so operator_log and operator_step_mapping staff numbers match."""
    return _norm(s).lstrip("e").lstrip("0") or "0"


def _machine_from_log_data(data, aliases: Optional[Dict[str, str]]) -> Optional[str]:
    """Machine id recorded in a dynamic form (any top-level key containing machine / eqpt / equip)."""
    if not isinstance(data, dict) or not aliases:
        return None
    for k, v in data.items():
        if not any(t in str(k).lower() for t in ("machine", "eqpt", "equip")):
            continue
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, dict):
                x = x.get("selectedValue") or x.get("value")
            rid = aliases.get(_norm(x))
            if rid:
                return rid
    return None


# ---------------------------------------- calendar / shifts -------------------------------
def _legacy_shifts(caps) -> List[ShiftDef]:
    """No rows in shift_definition: derive the shifts from stage_capacity exactly as before."""
    hps = [float(c.hours_per_shift) for c in caps if c.hours_per_shift and float(c.hours_per_shift) > 0]
    spd = [int(c.shifts_per_day) for c in caps if c.shifts_per_day and int(c.shifts_per_day) > 0]
    start_h = (getattr(caps[0], "shift_start_hour", None) or 6) if caps else 6
    per = max(hps) if hps else 7.0
    n = max(spd) if spd else 2
    work = min(per * n, 24 - start_h)
    each = work / n
    return [ShiftDef(f"Shift {k + 1}", int(round((start_h + k * each) * 60)),
                     int(round((start_h + (k + 1) * each) * 60)), True) for k in range(n)]


def _load_shifts(db: Session, caps) -> Tuple[List[ShiftDef], bool]:
    rows = db.query(ShiftDefinition).order_by(ShiftDefinition.sort_order, ShiftDefinition.id).all()
    if not rows:
        return _legacy_shifts(caps), True
    out = []
    for r in rows:
        s, e = _to_min(r.start_time), _to_min(r.end_time)
        out.append(ShiftDef(r.name, s, e if e > s else e + 1440, bool(r.is_active)))
    return out, False


def _load_changes(db: Session) -> List[ShiftChange]:
    out = []
    for r in db.query(ShiftAdjustment).order_by(ShiftAdjustment.id).all():
        out.append(ShiftChange(
            date_from=r.date_from, date_to=r.date_to or r.date_from, action=(r.action or "").lower(),
            shift_name=r.shift_name or None,
            start_min=_to_min(r.start_time) if r.start_time else None,
            end_min=_to_min(r.end_time) if r.end_time else None,
            operator=_norm_staff(r.operator_staff_no), note=r.note or ""))
    return out


def _calendar_json(cal: Calendar, public_hols, exc_rows, today: date) -> Dict[str, Any]:
    all_dates = [e.exception_date for e in exc_rows] + [h.holiday_date for h in public_hols]
    past = max(60, (today - min(all_dates)).days) if all_dates else 60
    future = max(240, (max(all_dates) - today).days) if all_dates else 240
    d_from, d_to = today - timedelta(days=past), today + timedelta(days=future)

    holidays: Dict[str, Dict[str, Any]] = {}
    for h in public_hols:
        holidays[h.holiday_date.isoformat()] = {"holiday_date": h.holiday_date.isoformat(), "name": h.name,
                                                "year": h.holiday_date.year}
    working_exceptions = []
    for e in exc_rows:
        etype, key = (e.exception_type or "").strip().lower(), e.exception_date.isoformat()
        if etype == "holiday" and key not in holidays:
            holidays[key] = {"holiday_date": key, "name": e.note or "Holiday", "year": e.exception_date.year}
        elif etype in ("working", "half-day"):
            holidays.pop(key, None)
            working_exceptions.append({"date": key, "type": e.exception_type, "note": e.note})
    d = d_from
    while d <= d_to:
        if cal.day_factor(d) == 0 and d.weekday() == 5 and d.isoformat() not in holidays:
            holidays[d.isoformat()] = {"holiday_date": d.isoformat(), "name": "Saturday off (1st/3rd)", "year": d.year}
        d += timedelta(days=1)

    day_windows, lo, hi = cal.day_windows_json(d_from, d_to)
    return {
        "shift_start": lo, "shift_end": hi,           # hull of every working window (drives the daily/weekly axis)
        "shifts": [{"name": s.name, "start": _hm(s.start_min), "end": _hm(s.end_min), "active": s.active}
                   for s in cal.shifts],
        "week_off": [0],
        "holidays": [holidays[k] for k in sorted(holidays)],
        "working_exceptions": working_exceptions,
        "day_windows": day_windows,                   # {"2026-10-13": [[360, 1320]]} minutes after midnight
    }


# ---------------------------------------- resources + interlocks --------------------------
def _stage_cfg(db: Session) -> Dict[int, Dict[str, Any]]:
    out = {}
    for r in db.query(StageScheduleConfig).all():
        out[r.stage_id] = {
            "batch_size": int(r.batch_size or DEFAULT_CFG["batch_size"]),
            "next_day_release": bool(r.next_day_release),
            "cooldown_minutes": float(r.cooldown_minutes or 0),
            "cooldown_working_time_only": bool(r.cooldown_working_time_only),
            "setup_minutes": float(r.setup_minutes or 0),
            "resource_mode": (r.resource_mode or "AUTO").upper(),
            "needs_operator": bool(r.needs_operator),
        }
    return out


def _build_resources(db, cal, stages, cfg, qualified, today):
    """-> (resources list in display order, stage_resources, stage_mode, alias map, warnings)"""
    warnings: List[str] = []
    plant = cal.plant_fn()
    equipments = db.query(Equipments).order_by(Equipments.stage_id, Equipments.id).all()
    machines_by_stage: Dict[int, List[Equipments]] = defaultdict(list)
    for e in equipments:
        if e.stage_id is not None:
            machines_by_stage[e.stage_id].append(e)

    def mode_of(sid: int) -> str:
        want = cfg.get(sid, DEFAULT_CFG)["resource_mode"]          # AUTO | MACHINE | MANUAL
        has_m, has_o = bool(machines_by_stage.get(sid)), bool(qualified.get(sid))
        if want == "MANUAL" and has_o:
            return "MANUAL"
        if want == "MACHINE" and has_m:
            return "MACHINE"
        return "MACHINE" if has_m else "MANUAL" if has_o else "FALLBACK"

    def machine_window(sid: Optional[int]) -> WinFn:
        if sid is not None and cfg.get(sid, DEFAULT_CFG)["needs_operator"]:
            ops = [q["staff"] for q in qualified.get(sid, []) if q["staff"]]
            if ops:
                return intersect_fn(plant, union_fn([cal.operator_fn(o) for o in ops]))
            stage = stages.get(sid)
            warnings.append(f"Stage '{stage.step_name if stage else sid}' needs an operator but none is qualified")
        return plant

    stage_mode = {sid: mode_of(sid) for sid in stages}
    resources: List[Resource] = []
    stage_resources: Dict[int, List[Resource]] = defaultdict(list)
    alias: Dict[str, List[Resource]] = defaultdict(list)
    by_mid: Dict[str, Resource] = {}

    # --- machines
    for e in equipments:
        if e.stage_id is not None and stage_mode.get(e.stage_id) == "MANUAL":
            continue
        mid = e.machine_id or e.eqpt_no or f"EQ-{e.id}"
        if mid in by_mid:
            if SHARE_MACHINE_ACROSS_STAGES:                  # FIX: same machine on another stage = same resource
                r = by_mid[mid]
                if e.stage_id is not None and e.stage_id not in r.meta["stages"]:
                    r.meta["stages"].add(e.stage_id)
                    if r.stage_id is None:
                        r.stage_id = e.stage_id
                    stage_resources[e.stage_id].append(r)
                continue
            mid = f"{mid}-{e.id}"
        stage = stages.get(e.stage_id)
        r = Resource(
            id=mid, name=e.eqpt_name or mid, kind="MACHINE",
            work_centre=e.workcenter_name or (stage.step_name if stage else ""), stage_id=e.stage_id,
            win_fn=machine_window(e.stage_id),
            meta={"eqpt_no": e.eqpt_no or "", "eqpt_name": e.eqpt_name or "", "eqpt_make": e.eqpt_make or "-",
                  "eqpt_model": e.eqpt_model or "-", "stages": {e.stage_id}})
        by_mid[mid] = r
        resources.append(r)
        if e.stage_id is not None:
            stage_resources[e.stage_id].append(r)
        for a in (e.machine_id, e.eqpt_no):
            if a:
                alias[_norm(a)].append(r)

    # --- operators (resource of MANUAL stages)
    op_res: Dict[str, Resource] = {}
    for sid, mode in stage_mode.items():
        if mode != "MANUAL":
            continue
        for q in qualified.get(sid, []):
            key = q["staff"] or q["name"]
            r = op_res.get(key)
            if r is None:
                r = Resource(id=f"OP-{key}", name=q["name"] or key, kind="MANUAL", work_centre="", stage_id=sid,
                             win_fn=cal.operator_fn(q["staff"]) if q["staff"] else plant,
                             operator=q["staff"], meta={"stages": set(), "names": []})
                op_res[key] = r
                resources.append(r)
                alias[_norm(key)].append(r)
            r.meta["stages"].add(sid)
            r.meta["names"].append(stages[sid].step_name)
            if q["expiry"]:
                r.valid_until[sid] = q["expiry"]
            stage_resources[sid].append(r)
    for r in op_res.values():
        r.work_centre = " / ".join(dict.fromkeys(r.meta["names"]))

    # --- stages nobody can serve -> visible "unassigned" row so the plan still has dates
    for sid, mode in stage_mode.items():
        if mode == "FALLBACK" and sid in stages:
            nm = stages[sid].step_name
            warnings.append(f"Stage '{nm}': no machine and no qualified operator - shown as Unassigned")
            r = Resource(id=f"UNASSIGNED-{sid}", name=f"{nm} (Unassigned)", kind="FALLBACK", work_centre=nm,
                         stage_id=sid, win_fn=plant, meta={"stages": {sid}})
            resources.append(r)
            stage_resources[sid].append(r)
    return resources, stage_resources, stage_mode, alias, warnings


def _qualified(db: Session) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    seen = set()
    for o in db.query(OperatorStepMapping).order_by(OperatorStepMapping.step_map_id).all():
        staff = _norm_staff(o.operator_staff_no)
        key = (o.flow_step_id, staff or o.operator_name)
        if not (staff or o.operator_name) or key in seen:
            continue
        seen.add(key)
        out[o.flow_step_id].append({"staff": staff, "name": o.operator_name or staff,
                                    "expiry": _parse_mrl_expiry(o.operator_MRL_Expiry)})
    return out


def _compute_interlocks(db, cal, now, resources, alias, stages, qualified, stage_resources):
    """Fills resource.blocks and returns the list shown on the Shifts & Interlocks tab."""
    items: List[Dict[str, Any]] = []
    today = now.date()
    horizon_end = now + timedelta(days=120)

    def add(res: Resource, typ: str, sev: str, msg: str, start: Optional[datetime], end: Optional[datetime]):
        items.append({"resource_id": res.id, "resource_name": res.name, "type": typ, "severity": sev,
                      "message": msg, "start_time": _iso(start) if start else None,
                      "end_time": _iso(end) if end else None})
        if sev == "BLOCK" and start:
            res.blocks.append((start, end, msg))

    def targets_of(machine_id) -> List[Resource]:
        return list({id(r): r for r in alias.get(_norm(machine_id), [])}.values())

    # 1. open breakdowns (machine is down until mc_ready_date is entered)
    for b in db.query(MachineBreakdownLog).all():
        if (b.status or "").strip().lower() in ("resolved", "closed") or b.mc_ready_date:
            end = _parse_dt(b.mc_ready_date)
            if not end or end <= now:
                continue
        else:
            end = None
        start = _parse_dt(b.breakdown_date, b.breakdown_time) or now
        for r in targets_of(b.machine_id):
            add(r, "BREAKDOWN", "BLOCK", f"Breakdown: {b.issue_description or 'machine down'}", min(start, now), end)

    # 2. planned preventive maintenance
    first_start = min((sh.start_min for sh in cal.shifts if sh.active), default=360)
    for m in db.query(MaintenanceScheduleLog).filter(
            MaintenanceScheduleLog.year.in_([today.year - 1, today.year, today.year + 1])).all():
        for q in (1, 2, 3, 4):
            if (getattr(m, f"q{q}_status") or "").strip().lower() in ("completed", "complete", "done", "closed"):
                continue
            d = _parse_date(getattr(m, f"q{q}_planned_date"))
            if not d:
                continue
            for r in targets_of(m.machine_id):
                if d < today:
                    add(r, "MAINTENANCE", "WARN", f"Preventive maintenance overdue since {d:%d %b %Y}", None, None)
                elif datetime(d.year, d.month, d.day) < horizon_end:
                    s = datetime(d.year, d.month, d.day) + timedelta(minutes=first_start)
                    add(r, "MAINTENANCE", "BLOCK", f"Planned maintenance on {d:%d %b %Y}", s,
                        s + timedelta(hours=PM_DOWNTIME_HOURS))

    # 3. spares + machine consumables (inventory_spares is per equipment)
    for it in db.query(InventorySpares).all():
        qty, thr = _num(it.qty_avl), _num(it.threshold_qty)
        if qty is None:
            continue
        stype = "CONSUMABLE" if "consumable" in str(it.sparetype or "").lower() else "SPARE"
        targets = targets_of(it.eqpt_no) if it.eqpt_no else \
            [r for r in stage_resources.get(it.flow_step_id, []) if r.kind == "MACHINE"]
        what = f"{it.part_number or ''} {it.decription or ''}".strip()
        lead = _days(it.procurement_lead_time)
        for r in targets:
            if qty <= 0:
                end = now + timedelta(days=lead) if lead and lead > 0 else None
                msg = f"{stype.title()} out of stock: {what}" + (f" (procurement {lead:g} d)" if end else " (no ETA)")
                add(r, stype, "BLOCK", msg, now, end)
            elif thr is not None and qty < thr:
                sev = "BLOCK" if BLOCK_WHEN_BELOW_THRESHOLD else "WARN"
                add(r, stype, sev, f"{stype.title()} below threshold ({qty:g} < {thr:g}): {what}", now, None)

    # 4. stage consumables (e.g. solder paste): expired / empty lots stop the whole stage
    lots: Dict[Tuple[int, str], List[Any]] = defaultdict(list)
    for c in db.query(Consumables).all():
        if c.is_consumable_present is False or c.stage_id is None:
            continue
        lots[(c.stage_id, c.consumable_material_number or c.consumable_material_name or "?")].append(c)
    for (sid, mat), rows in lots.items():
        usable = [c for c in rows if (c.quantity or 0) > 0 and (not c.consumable_shelf_life or c.consumable_shelf_life >= today)]
        name = rows[0].consumable_material_name or mat
        targets = [r for r in stage_resources.get(sid, []) if r.kind == "MACHINE"]
        if not usable:
            for r in targets:
                add(r, "CONSUMABLE", "BLOCK", f"No usable stock of {name} (empty / shelf life expired)", now, None)
        else:
            dated = [c.consumable_shelf_life for c in usable if c.consumable_shelf_life]
            if dated and len(dated) == len(usable) and max(dated) <= today + timedelta(days=CONSUMABLE_WARN_DAYS):
                for r in targets:
                    add(r, "CONSUMABLE", "WARN", f"Last usable lot of {name} expires on {max(dated):%d %b %Y}", None, None)

    # 5. operator qualification (MRL) - the scheduler also refuses expired operators
    for sid, lst in qualified.items():
        for q in lst:
            if not q["expiry"]:
                continue
            res = next((r for r in alias.get(_norm(q["staff"] or q["name"]), []) if r.kind == "MANUAL"), None)
            label = stages[sid].step_name if sid in stages else sid
            exp_day = (q["expiry"] - timedelta(days=1)).date()
            holder = res or Resource(id=f"OP-{q['staff'] or q['name']}", name=q["name"], kind="MANUAL",
                                     work_centre="", stage_id=sid, win_fn=lambda d: [])
            if exp_day < today:
                items.append({"resource_id": holder.id, "resource_name": holder.name, "type": "OPERATOR_MRL",
                              "severity": "BLOCK", "message": f"Qualification for '{label}' expired on {exp_day:%d %b %Y}",
                              "start_time": None, "end_time": None})
            elif (exp_day - today).days <= MRL_WARN_DAYS:
                items.append({"resource_id": holder.id, "resource_name": holder.name, "type": "OPERATOR_MRL",
                              "severity": "WARN", "message": f"Qualification for '{label}' expires on {exp_day:%d %b %Y}",
                              "start_time": None, "end_time": None})
    items.sort(key=lambda i: (i["severity"] != "BLOCK", i["resource_name"]))
    return items


# ---------------------------------------- the schedule ------------------------------------
def build_schedule(db: Session, time_source: str = "actual", history_days: int = 7,
                   plant: Optional[str] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = (now or datetime.now()).replace(microsecond=0)
    today = now.date()
    warnings: List[str] = []

    # ---- calendar: shifts (+ runtime changes), holidays
    caps = db.query(StageCapacityDB).all()
    shifts, derived = _load_shifts(db, caps)
    changes = _load_changes(db)
    op_shifts: Dict[str, List[str]] = defaultdict(list)
    for a in db.query(OperatorShiftAssignment).all():
        if _norm_staff(a.operator_staff_no):
            op_shifts[_norm_staff(a.operator_staff_no)].append(a.shift_name)
    public_hols = db.query(PublicHoliday).order_by(PublicHoliday.holiday_date).all()
    exc_rows = db.query(CalendarException).order_by(CalendarException.exception_date).all()
    cal = Calendar(public_holidays=[h.holiday_date for h in public_hols],
                   exceptions={e.exception_date: e.exception_type for e in exc_rows},
                   shifts=shifts, changes=changes, operator_shifts=op_shifts)
    calendar_json = _calendar_json(cal, public_hols, exc_rows, today)

    # ---- master data
    stages = {s.flow_step_id: s for s in db.query(ProcessFlowMaster).all()}
    batch_by_stage = {p.stage_id: bool(p.process_type_batch) for p in db.query(Processtype).all()}
    cfg = _stage_cfg(db)
    qualified = _qualified(db)
    cap_by_name = {(c.name or "").strip().lower(): c for c in caps}

    resources, stage_resources, stage_mode, alias, w = _build_resources(db, cal, stages, cfg, qualified, today)
    warnings += w
    interlocks = _compute_interlocks(db, cal, now, resources, alias, stages, qualified, stage_resources)
    by_id = {r.id: r for r in resources}

    # per stage: machine alias -> resource id (machine_id / eqpt_no only - machine names repeat)
    stage_alias: Dict[int, Dict[str, str]] = {}
    for sid, lst in stage_resources.items():
        stage_alias[sid] = {_norm(a): r.id for r in lst if r.kind == "MACHINE"
                            for a in (r.id, r.meta.get("eqpt_no")) if a}

    # ---- production orders, quantities, progress
    pcb_rows = db.query(PcbData.PCBserialNoPartNumber, PcbData.productionOrder, PcbData.partNumber,
                        PcbData.quantity, PcbData.createdAt, PcbData.description
                        ).filter(PcbData.productionOrder.isnot(None), PcbData.productionOrder != "").all()
    po_pcbs: Dict[str, list] = defaultdict(list)
    pcb_weight: Dict[str, tuple] = {}
    for r in pcb_rows:
        wgt = r.quantity if (r.quantity and r.quantity > 0) else 1
        po_pcbs[r.productionOrder].append(r)
        pcb_weight[r.PCBserialNoPartNumber] = (r.productionOrder, wgt)
    tracking = {t.po_number: t for t in db.query(ProductionOrderTracking).all()}

    def _empty_prog():
        return {"done": 0, "started": 0, "start": None, "end": None,
                "ops": Counter(), "staff": Counter(), "machines": Counter()}

    prog: Dict[tuple, Dict[str, Any]] = {}
    serial_rows: List[Dict[str, Any]] = []          # one row per serial + stage: who worked on it and when
    stale_seen: Counter = Counter()
    log_rows = db.query(OperatorLog.PCBserialNoPartNumber, OperatorLog.current_step_id, OperatorLog.Task_Status,
                        OperatorLog.start_time, OperatorLog.end_time, OperatorLog.operator_name,
                        OperatorLog.operator_staff_no, OperatorLog.log_Data
                        ).join(PcbData, PcbData.PCBserialNoPartNumber == OperatorLog.PCBserialNoPartNumber
                               ).filter(PcbData.productionOrder.isnot(None)).all()
    for lg in log_rows:
        po_w = pcb_weight.get(lg.PCBserialNoPartNumber)
        if not po_w:
            continue
        po_no, wgt = po_w
        st_t, en_t = _as_log_time(lg.start_time), _as_log_time(lg.end_time)
        p = prog.setdefault((po_no, lg.current_step_id), _empty_prog())
        status = (lg.Task_Status or "").strip().upper().replace("-", "_")
        if status in COMPLETED_STATUSES:
            p["done"] += wgt
            if en_t and (p["end"] is None or en_t > p["end"]):
                p["end"] = en_t
        else:
            p["started"] += wgt
        if st_t and (p["start"] is None or st_t < p["start"]):
            p["start"] = st_t
        if lg.operator_name:
            p["ops"][lg.operator_name] += 1
        if lg.operator_staff_no:
            p["staff"][_staff_key(lg.operator_staff_no)] += 1
        m = _machine_from_log_data(lg.log_Data, stage_alias.get(lg.current_step_id))
        if m:
            p["machines"][m] += 1
        # time this serial really occupied the operator: start -> completion / pause / now (if still running)
        if st_t:
            last_action = en_t or st_t
            running = False
            if status in COMPLETED_STATUSES or status not in RUNNING_STATUSES:
                fin = en_t                                     # completed, paused, rework raised ...
            elif now - last_action <= timedelta(hours=STALE_RUNNING_HOURS):
                fin, running = now, True                       # being worked on right now
            else:
                fin = en_t                                     # forgotten "Started" scan: does not occupy now
                stale_seen[po_no] += 1
            if fin and fin > st_t:
                serial_rows.append({"po": po_no, "sid": lg.current_step_id, "start": st_t, "end": fin, "wgt": wgt,
                                    "staff": _staff_key(lg.operator_staff_no) if lg.operator_staff_no else None,
                                    "running": running, "done": status in COMPLETED_STATUSES})
    if stale_seen:
        pos = sorted(stale_seen)
        warnings.append(f"{sum(stale_seen.values())} serial(s) in {len(pos)} PO(s) still 'Started' with no action for "
                        f"more than {STALE_RUNNING_HOURS:g} h - not counted as running now (forgotten scans?): "
                        f"{', '.join(pos[:15])}{' ...' if len(pos) > 15 else ''}")


    # SPC entries also tell which machine a serial / PO ran on
    serial_to_po: Dict[str, str] = {}
    for r in pcb_rows:
        serial_to_po[_norm(r.PCBserialNoPartNumber)] = r.productionOrder
        serial_to_po[_norm(str(r.PCBserialNoPartNumber).split("$")[0])] = r.productionOrder
    spc_machine: Dict[Tuple[str, int], str] = {}
    for s in db.query(SPCDataLog.referral_number, SPCDataLog.stage_id, SPCDataLog.machine_id).all():
        po = serial_to_po.get(_norm(s.referral_number)) or (s.referral_number if s.referral_number in po_pcbs else None)
        rid = stage_alias.get(s.stage_id, {}).get(_norm(s.machine_id))
        if po and rid:
            spc_machine[(po, s.stage_id)] = rid

    po_keys = sorted(set(po_pcbs) | set(tracking))
    po_part: Dict[str, str] = {}
    for po_no in po_keys:
        parts = Counter(r.partNumber for r in po_pcbs.get(po_no, []) if r.partNumber)
        part = parts.most_common(1)[0][0] if parts else (tracking[po_no].part_number if po_no in tracking else None)
        if part:
            po_part[po_no] = part
    part_numbers = set(po_part.values())
    products = {p.part_number: p for p in db.query(ProductTypeMaster).filter(
        ProductTypeMaster.part_number.in_(part_numbers)).all()} if part_numbers else {}
    routing: Dict[str, list] = defaultdict(list)
    if part_numbers:
        for r in db.query(ProductWorkflowMapping).filter(
                ProductWorkflowMapping.product_part_number.in_(part_numbers)
        ).order_by(ProductWorkflowMapping.product_part_number, ProductWorkflowMapping.step_sequence).all():
            routing[r.product_part_number].append(r)
    times: Dict[tuple, list] = defaultdict(list)
    if part_numbers:
        for t in db.query(PartProcessTime).filter(PartProcessTime.part_no.in_(part_numbers)).all():
            times[(t.part_no, t.process_id)].append(t)
    other = "sap" if time_source == "actual" else "actual"

    def hours_for(part: str, step_id: int, route_row=None):
        """hours per PCB (series stage) or per batch (batch stage)"""
        rows = times.get((part, step_id), [])
        if rows:
            plant_code = products[part].plant_code if part in products else None
            row = next((r for r in rows if r.plant == plant_code), rows[0])
            for src in (time_source, other):
                val = row.actual_time if src == "actual" else row.sap_time
                if val and val > 0:
                    return float(val), src
        if route_row is not None and getattr(route_row, "process_time_seconds", None):
            return route_row.process_time_seconds / 3600.0, "routing"
        stage = stages.get(step_id)
        cap = cap_by_name.get((stage.step_name or "").strip().lower()) if stage else None
        if cap and cap.manufacturing_time_mins and cap.manufacturing_time_mins > 0:
            return float(cap.manufacturing_time_mins) / 60.0, "stage_capacity"
        return None, "default"

    orders: Dict[str, Dict[str, Any]] = {}
    missing_time_seen: set = set()
    for po_no in po_keys:
        part = po_part.get(po_no)
        trk = tracking.get(po_no)
        if not part:
            warnings.append(f"PO {po_no}: no part number")
            continue
        if plant and part in products and products[part].plant_code != plant:
            continue
        steps = routing.get(part, [])
        if not steps:
            warnings.append(f"PO {po_no}: part {part} has no routing in product_workflow_mapping")
            continue
        rows = po_pcbs.get(po_no, [])
        pcb_qty = sum((r.quantity if (r.quantity and r.quantity > 0) else 1) for r in rows)
        qty = max(pcb_qty, (trk.target_quantity if trk and trk.target_quantity else 0))
        if qty <= 0:
            warnings.append(f"PO {po_no}: quantity is 0")
            continue
        created = [r.createdAt for r in rows if r.createdAt]
        release = min(created) if created else ((trk.started_at or trk.created_at) if trk else None) or now
        description = (products[part].description if part in products and products[part].description
                       else next((r.description for r in rows if r.description), "")) or ""
        ops = []
        for idx, st in enumerate(steps, start=1):
            seq = st.step_sequence or idx
            sid = st.flow_step_id
            stage = stages.get(sid)
            c = {**DEFAULT_CFG, **cfg.get(sid, {})}
            mode = "BATCH" if batch_by_stage.get(sid) else "SERIES"
            hrs, src = hours_for(part, sid, st)
            if hrs is None and (part, sid) not in missing_time_seen:      # one warning per part + step, not per PO
                missing_time_seen.add((part, sid))
                warnings.append(f"Part {part}: no process time for '{stage.step_name if stage else sid}' "
                                f"(part_process_time / routing / stage_capacity) - 1 min assumed")
            unit = hrs * 60.0 if hrs else 1.0                     # minutes per PCB (series) / per batch (batch)
            p = prog.get((po_no, sid)) or _empty_prog()
            ops.append({
                "op_id": f"{po_no}-{seq * 10}", "production_order": po_no, "part_number": part,
                "part_description": description, "operation_no": seq * 10, "flow_step_id": sid,
                "step_name": stage.step_name if stage else f"Step {sid}", "mode": mode, "quantity": qty,
                "unit_minutes": unit, "batch_size": min(int(c["batch_size"]), RULES["max_batch_size"]),
                "setup_minutes": c["setup_minutes"], "cooldown_minutes": c["cooldown_minutes"],
                "cooldown_working_only": c["cooldown_working_time_only"], "next_day_release": c["next_day_release"],
                "time_source_used": src, "done": min(p["done"], qty), "started": p["started"],
                "actual_start": p["start"], "actual_end": p["end"],
                "logged_operator": p["ops"].most_common(1)[0][0] if p["ops"] else "",
                "logged_staff": p["staff"], "logged_machines": p["machines"],
            })
        all_done = all(o["done"] >= qty for o in ops)
        closed = bool(trk and (trk.status or "").strip().lower() in CLOSED_PO_STATUSES)
        if all_done or closed:
            ends = [o["actual_end"] for o in ops if o["actual_end"]]
            if closed and trk:
                ends.append(trk.closed_at or trk.updated_at or release)
            if (now - (max(ends) if ends else release)) > timedelta(days=history_days):
                continue
        orders[po_no] = {"production_order": po_no, "part_number": part, "description": description,
                         "quantity": qty, "release": release, "ops": ops}

    # a routing stage that has no resource at all still needs one (should not happen after _build_resources)
    for po in orders.values():
        for op in po["ops"]:
            if not stage_resources.get(op["flow_step_id"]):
                r = Resource(id=f"UNASSIGNED-{op['flow_step_id']}", name=f"{op['step_name']} (Unassigned)",
                             kind="FALLBACK", work_centre=op["step_name"], stage_id=op["flow_step_id"],
                             win_fn=cal.plant_fn(), meta={"stages": {op["flow_step_id"]}})
                resources.append(r)
                by_id[r.id] = r
                stage_resources[op["flow_step_id"]].append(r)

    # operator name shown on machine operations (logged one, else rotate through the qualified pool)
    for i, po in enumerate(orders.values()):
        for op in po["ops"]:
            pool = [q["name"] for q in qualified.get(op["flow_step_id"], []) if q["name"]]
            op["display_operator"] = op["logged_operator"] or (pool[i % len(pool)] if pool else "")

    # ---- which resource did started / finished work really use? (no extra table needed)
    def actual_resource(op) -> Tuple[Optional[str], str]:
        sid, cands = op["flow_step_id"], stage_resources.get(op["flow_step_id"], [])
        if stage_mode.get(sid) == "MANUAL":                                   # 1. operator is the resource
            by_staff = {_staff_key(r.operator): r.id for r in cands if r.kind == "MANUAL" and r.operator}
            for staff, _ in op["logged_staff"].most_common():
                if staff in by_staff:
                    return by_staff[staff], "operator_log"
        if len(cands) == 1:                                                   # 2. only one resource at the stage
            return cands[0].id, "single_resource"
        if op["logged_machines"]:                                             # 3. machine recorded in the form
            return op["logged_machines"].most_common(1)[0][0], "log_data"
        rid = spc_machine.get((op["production_order"], sid))                  # 4. SPC entry
        if rid:
            return rid, "spc_log"
        return None, "baseline_plan"                                          # 5. guess (old behaviour)

    # ---- schedule: baseline plan, then current plan (history + running work fixed, manual moves pinned)
    sequence = sorted(orders, key=lambda k: (orders[k]["release"], k))
    plan = run_scheduler(cal, stage_resources, orders, sequence, rules=RULES)

    fixed: Dict[str, Dict[str, Any]] = {}
    for po in orders.values():
        qty = po["quantity"]
        for op in po["ops"]:
            if not (op["done"] > 0 or op["started"] > 0):
                op["resource_source"] = "planner"
                continue
            first = plan[op["op_id"]][0]
            rid, src = actual_resource(op)
            op["resource_source"] = src
            res_id = rid or first["resource_id"]
            if op["done"] >= qty:
                fixed[op["op_id"]] = {"resource_id": res_id, "confirmed": rid is not None,
                                      "start": op["actual_start"] or first["start"],
                                      "end": op["actual_end"] or max(t["end"] for t in plan[op["op_id"]])}
            else:
                if op["mode"] == "BATCH":
                    size = max(1, op["batch_size"])
                    left = math.ceil((qty - op["done"]) / size) if op["done"] else math.ceil(qty / size)
                    rem = left * op["unit_minutes"]
                else:
                    rem = (qty - op["done"]) * op["unit_minutes"] if op["done"] else qty * op["unit_minutes"]
                fixed[op["op_id"]] = {"resource_id": res_id, "confirmed": rid is not None,
                                      "start": op["actual_start"] or now, "remaining_minutes": max(rem, 1.0)}

    pins: Dict[str, Dict[str, Any]] = {}
    for tid, ov in {o.op_id: o for o in db.query(ScheduleOverride).all()}.items():
        if ov.machine_id and ov.machine_id in by_id:
            try:
                pins[tid] = {"resource_id": ov.machine_id,
                             "start": datetime.fromisoformat(ov.start_time) if ov.start_time else None}
            except ValueError:
                continue

    cur = run_scheduler(cal, stage_resources, orders, sequence, fixed=fixed, pins=pins, not_before=now, rules=RULES)

    # ---- started / finished operations: draw what really happened, serial by serial, then the queued rest.
    # A resource (machine, or operator at a MANUAL stage) does one serial at a time:
    #   - a serial's log can span days (paused, rework, a scan left "Started"); it is drawn as at most
    #     HISTORY_MAX_FACTOR x the standard time, ending at its completion (or now if it is running)
    #   - each serial is credited only the time after the previous completion on the same resource
    #   - machine unknown and several machines at the stage: the serial goes to a machine that was free
    op_by_key = {(op["production_order"], op["flow_step_id"]): op for po in orders.values() for op in po["ops"]}
    confirmed_src = {"operator_log", "single_resource", "log_data", "spc_log"}
    staff_res_cache: Dict[int, Dict[str, str]] = {}

    def staff_res(sid: int) -> Dict[str, str]:
        if sid not in staff_res_cache:
            staff_res_cache[sid] = {_staff_key(r.operator): r.id for r in stage_resources.get(sid, [])
                                    if r.kind == "MANUAL" and r.operator}
        return staff_res_cache[sid]

    long_logs: Counter = Counter()
    hrows: List[Dict[str, Any]] = []
    for r in serial_rows:
        op = op_by_key.get((r["po"], r["sid"]))
        if not op or op["op_id"] not in fixed or len(cur.get(op["op_id"], [])) != 1:
            continue
        t = cur[op["op_id"]][0]
        st, en = r["start"], r["end"]
        if HISTORY_MAX_FACTOR > 0:
            per = op["unit_minutes"] * (1 if op["mode"] == "BATCH" else (r["wgt"] or 1))
            cap = timedelta(minutes=max(per * HISTORY_MAX_FACTOR, HISTORY_MIN_MINUTES))
            if en - st > cap:
                st = en - cap
                long_logs[op["step_name"]] += 1
        sid = r["sid"]
        if stage_mode.get(sid) == "MANUAL":
            rid = staff_res(sid).get(r["staff"]) if r["staff"] else None
            rid = rid or t["resource_id"]
        elif op.get("resource_source") in confirmed_src or len(stage_resources.get(sid, [])) <= 1:
            rid = t["resource_id"]
        else:
            rid = None                                       # machine not known: pick a free one below
        hrows.append({"op": op, "start": st, "end": en, "rid": rid, "pref": t["resource_id"],
                      "cands": [c.id for c in stage_resources.get(sid, [])] or [t["resource_id"]],
                      "running": r["running"], "done": r["done"]})
    for name, n in long_logs.items():
        warnings.append(f"'{name}': {n} serial log(s) last much longer than the standard time (paused, rework or a "
                        f"scan left open) - drawn as at most {HISTORY_MAX_FACTOR:g}x standard time before completion")

    hrows.sort(key=lambda x: (x["end"], x["start"]))
    last_end: Dict[str, datetime] = defaultdict(lambda: datetime.min)
    credited: Dict[str, List[Tuple[datetime, datetime, Dict[str, Any]]]] = defaultdict(list)
    for x in hrows:
        rid = x["rid"]
        if rid is None:
            free = [c for c in x["cands"] if last_end[c] <= x["start"]]
            rid = x["pref"] if x["pref"] in free else (free[0] if free else min(x["cands"], key=lambda c: last_end[c]))
        a = max(x["start"], last_end[rid])
        last_end[rid] = max(last_end[rid], x["end"])
        credited[rid].append((a, x["end"], x))

    gap = timedelta(minutes=HISTORY_MERGE_GAP_MINUTES)
    history: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rid, lst in credited.items():
        blk = None
        for a, b, x in lst:
            oid = x["op"]["op_id"]
            if blk and blk["op_id"] == oid and a - blk["end"] <= gap:
                blk["end"] = max(blk["end"], b)
                blk["serials"] += 1
                blk["running"] |= x["running"]
                blk["all_done"] &= x["done"]
                continue
            if b <= a:
                continue                                     # fully inside other work on this resource
            blk = {"op_id": oid, "rid": rid, "start": a, "end": b, "serials": 1,
                   "running": x["running"], "all_done": x["done"]}
            history[oid].append(blk)

    for po in orders.values():
        for op in po["ops"]:
            tasks = cur.get(op["op_id"], [])
            if op["op_id"] not in fixed or len(tasks) != 1 or not tasks[0]["fixed"]:
                continue
            t = tasks[0]
            blocks = history.get(op["op_id"], [])
            new = t.get("new_segments") or []
            if not blocks and (op["done"] >= op["quantity"] or not new):
                continue                                     # finished but no usable times: keep as it was
            new_list = []
            for i, b in enumerate(sorted(blocks, key=lambda b: b["start"]), start=1):
                new_list.append(dict(t, task_id=f"{op['op_id']}-H{i}", resource_id=b["rid"], start=b["start"],
                                     end=b["end"], segments=[(b["start"], b["end"])], qty=b["serials"], batch_no=i,
                                     working_minutes=round((b["end"] - b["start"]).total_seconds() / 60.0, 1),
                                     history=True, running=b["running"], all_done=b["all_done"], new_segments=[]))
            if op["done"] < op["quantity"] and new:          # the rest, queued - never a bar from the first scan
                rest_q = op["quantity"] - op["done"]
                parts = t.get("remaining_parts") or [{"kind": "AVAILABLE", "segments": new, "share": 1.0}]
                for j, pt in enumerate(parts):
                    sg = pt["segments"]
                    new_list.append(dict(t, task_id=op["op_id"] if j == 0 else f"{op['op_id']}-W", start=sg[0][0],
                                         end=sg[-1][1], segments=sg, remaining=True, flow_checked=True,
                                         remaining_kind=pt["kind"], waiting_reason=pt.get("reason", ""),
                                         working_minutes=round(sum((y - x).total_seconds() for x, y in sg) / 60.0, 1),
                                         qty=max(1, round(rest_q * pt["share"]))))
            for x in new_list:
                x["batch_count"] = len(new_list)
            cur[op["op_id"]] = new_list
    notes = detect_conflicts(orders, cur, by_id)

    # ---- JSON
    plan_by_task = {t["task_id"]: t for ts in plan.values() for t in ts}
    operations: List[Dict[str, Any]] = []
    for po in orders.values():
        qty = po["quantity"]
        for op in po["ops"]:
            pl_all = plan[op["op_id"]]
            if op["done"] >= qty:
                op_status = "COMPLETED"
            elif op["done"] > 0 or op["started"] > 0:
                op_status = "IN_PROGRESS"
            else:
                op_status = "SCHEDULED"
            for t in cur[op["op_id"]]:
                res = by_id[t["resource_id"]]
                if t.get("history"):
                    entry_type = "HISTORY"
                    status = "IN_PROGRESS" if t["running"] or not t["all_done"] else "COMPLETED"
                else:
                    entry_type = "REMAINING" if t.get("remaining") or (t["fixed"] and op_status != "COMPLETED") \
                        else "HISTORY" if t["fixed"] else "PLAN"
                    status = op_status
                pt = plan_by_task.get(t["task_id"])
                if pt is None:                                    # running/finished op: show the whole planned span
                    pt = {"resource_id": pl_all[0]["resource_id"], "start": min(x["start"] for x in pl_all),
                          "end": max(x["end"] for x in pl_all),
                          "segments": [s for x in pl_all for s in x["segments"]]}
                conflicts = list(notes.get(t["task_id"], []))
                if op["flow_step_id"] not in res.meta.get("stages", {res.stage_id}):
                    conflicts.append(f"{res.name} is not set up for '{op['step_name']}'")
                operations.append({
                    "op_id": t["task_id"], "parent_op_id": op["op_id"],
                    "production_order": op["production_order"], "part_number": op["part_number"],
                    "part_description": op["part_description"], "operation_no": op["operation_no"],
                    "flow_step_id": op["flow_step_id"], "step_name": op["step_name"], "operation_name": op["step_name"],
                    "process_mode": op["mode"], "quantity": qty, "completed_qty": op["done"],
                    "serial_count": t["qty"] if t.get("history") else None,
                    "batch_no": t["batch_no"], "batch_count": t["batch_count"], "batch_qty": t["qty"],
                    "batch_size": op["batch_size"] if op["mode"] == "BATCH" else None,
                    "duration_hours": round(t["working_minutes"] / 60.0, 2),
                    "setup_minutes": t["setup_minutes"], "cooldown_minutes": op["cooldown_minutes"],
                    "status": status, "op_status": op_status,
                    "entry_type": entry_type,      # HISTORY (logged serials) | REMAINING (queued rest) | PLAN
                    "remaining_kind": t.get("remaining_kind"),   # AVAILABLE (PCBs here now) | WAITING (from prev stage)
                    "operator_name": res.name if res.kind == "MANUAL" else op["display_operator"],
                    "resource_type": res.kind,
                    "resource_source": op.get("resource_source"),   # planner | operator_log | single_resource |
                                                                    # log_data | spc_log | baseline_plan
                    "machine_id": t["resource_id"], "start_time": _iso(t["start"]), "end_time": _iso(t["end"]),
                    "segments": [[_iso(a), _iso(b)] for a, b in t["segments"]],
                    "planned_machine_id": pt["resource_id"], "planned_start": _iso(pt["start"]),
                    "planned_end": _iso(pt["end"]),
                    "planned_segments": [[_iso(a), _iso(b)] for a, b in pt["segments"]],
                    "time_source_used": op["time_source_used"], "waiting_reason": t["waiting_reason"],
                    "interlock_blocked": t["blocked"], "interlock_reasons": t["block_reasons"],
                    "pinned": t["pinned"], "conflicts": conflicts,
                })
    operations.sort(key=lambda o: o["start_time"])

    # ---- machine rows (resource_type tells the front end: MACHINE | MANUAL | FALLBACK)
    def resource_state(r: Resource) -> str:
        mine = [i for i in interlocks if i["resource_id"] == r.id]
        active = [i for i in mine if i["severity"] == "BLOCK" and (i["start_time"] is None or i["start_time"] <= _iso(now))
                  and (i["end_time"] is None or i["end_time"] > _iso(now))]
        return "BLOCKED" if active else "WARN" if mine else "OK"

    def order_key(r: Resource):
        s = stages.get(r.stage_id)
        return (s.step_order if s and s.step_order is not None else 10 ** 6, r.kind != "MACHINE", r.id)

    machines = []
    for r in sorted(resources, key=order_key):
        machines.append({"machine_id": r.id, "work_centre": r.work_centre, "machine_name": r.name,
                         "resource_type": r.kind, "stage_id": r.stage_id,
                         "stage_ids": sorted(s for s in r.meta.get("stages", {r.stage_id}) if s is not None),
                         "interlock_status": resource_state(r),
                         "eqpt_no": r.meta.get("eqpt_no", ""), "eqpt_name": r.meta.get("eqpt_name", ""),
                         "eqpt_make": r.meta.get("eqpt_make", "-"), "eqpt_model": r.meta.get("eqpt_model", "-")})

    operators = []
    seen_staff = set()
    for sid, lst in qualified.items():
        for q in lst:
            if q["staff"] and q["staff"] not in seen_staff:
                seen_staff.add(q["staff"])
                operators.append({"staff_no": q["staff"], "name": q["name"], "shifts": op_shifts.get(q["staff"], [])})
    operators.sort(key=lambda o: o["name"] or "")

    return {
        "generated_at": _iso(now), "calendar": calendar_json, "machines": machines,
        "production_orders": [{"production_order": po["production_order"], "part_number": po["part_number"],
                               "description": po["description"], "quantity": po["quantity"],
                               "release_date": _iso(po["release"])}
                              for po in sorted(orders.values(), key=lambda p: (p["release"], p["production_order"]))],
        "operations": operations, "interlocks": interlocks, "operators": operators,
        "shifts_derived": derived, "warnings": list(dict.fromkeys(warnings)),
    }


# ---------------------------------------- request bodies ----------------------------------
class OpChange(BaseModel):
    op_id: str
    machine_id: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None


class UpdatePayload(BaseModel):
    changes: List[OpChange]


class ConfigPayload(BaseModel):
    stage_id: int
    batch_size: int = 30
    next_day_release: bool = False
    cooldown_minutes: float = 0
    cooldown_working_time_only: bool = False
    setup_minutes: float = 0
    resource_mode: str = "AUTO"
    needs_operator: bool = False


class ShiftPayload(BaseModel):
    id: Optional[int] = None
    name: str
    start_time: str
    end_time: str
    is_active: bool = True
    sort_order: int = 0


class ShiftChangePayload(BaseModel):
    date_from: date
    date_to: Optional[date] = None
    action: str                              # disable | enable | add
    shift_name: Optional[str] = None
    start_time: Optional[str] = None         # add: custom hours
    end_time: Optional[str] = None
    operator_staff_no: Optional[str] = None  # empty = whole plant
    note: Optional[str] = None


class OperatorShiftsPayload(BaseModel):
    shifts: List[str]


class ClearPayload(BaseModel):
    op_ids: Optional[List[str]] = None


# ---------------------------------------- endpoints ---------------------------------------
def create_schedule_router(init_db, verify_token=None) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_token)] if verify_token else [])

    @router.get("/schedule/data")
    def get_schedule_data(time_source: str = "actual", history_days: int = 7, plant: Optional[str] = None,
                          db: Session = Depends(init_db)):
        """Everything the schedule tabs need in one call. The plan is recalculated on every call, so a
        shift change / breakdown / stock update is visible as soon as the screen refreshes."""
        if time_source not in ("actual", "sap"):
            raise HTTPException(status_code=400, detail="time_source must be 'actual' or 'sap'")
        if history_days < 0:
            raise HTTPException(status_code=400, detail="history_days cannot be negative")
        try:
            return build_schedule(db, time_source=time_source, history_days=history_days, plant=plant)
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/schedule/update")
    def update_schedule(payload: UpdatePayload, db: Session = Depends(init_db)):
        """Manual moves from the Machine Schedule 'Update' button (op_id = task id, e.g. 83086577-40-B2).
        The task is pinned to that resource/start; later operations are re-planned around it."""
        updated = 0
        for ch in payload.changes:
            for label, val in (("start_time", ch.start_time), ("end_time", ch.end_time)):
                if val:
                    try:
                        datetime.fromisoformat(val)
                    except ValueError:
                        raise HTTPException(status_code=400, detail=f"{ch.op_id}: {label} must be ISO YYYY-MM-DDTHH:MM:SS")
            if not (ch.machine_id or ch.start_time or ch.end_time):
                continue
            row = db.query(ScheduleOverride).filter(ScheduleOverride.op_id == ch.op_id).first()
            if not row:
                row = ScheduleOverride(op_id=ch.op_id)
                db.add(row)
            for f in ("machine_id", "start_time", "end_time"):
                v = getattr(ch, f)
                if v:
                    setattr(row, f, v)
            updated += 1
        db.commit()
        return {"updated": updated}

    @router.post("/schedule/overrides/clear")
    def clear_overrides(payload: ClearPayload, db: Session = Depends(init_db)):
        q = db.query(ScheduleOverride)
        if payload.op_ids:
            q = q.filter(ScheduleOverride.op_id.in_(payload.op_ids))
        n = q.delete(synchronize_session=False)
        db.commit()
        return {"cleared": n}

    # ---------------- process config (batch size, setup, cooldown, resource mode) ----------------
    @router.get("/schedule/config")
    def get_config(db: Session = Depends(init_db)):
        cfg = _stage_cfg(db)
        batch = {p.stage_id: bool(p.process_type_batch) for p in db.query(Processtype).all()}
        rows = []
        for s in db.query(ProcessFlowMaster).order_by(ProcessFlowMaster.step_order, ProcessFlowMaster.flow_step_id).all():
            c = {**DEFAULT_CFG, **cfg.get(s.flow_step_id, {})}
            rows.append({"stage_id": s.flow_step_id, "stage_name": s.step_name,
                         "process_mode": "BATCH" if batch.get(s.flow_step_id) else "SERIES",
                         "is_default": s.flow_step_id not in cfg, **c})
        return rows

    @router.post("/schedule/config")
    def save_config(p: ConfigPayload, db: Session = Depends(init_db)):
        if not db.query(ProcessFlowMaster).filter(ProcessFlowMaster.flow_step_id == p.stage_id).first():
            raise HTTPException(status_code=404, detail="Unknown stage")
        if p.batch_size < 1 or p.batch_size > RULES["max_batch_size"]:
            raise HTTPException(status_code=400, detail=f"batch size must be between 1 and {RULES['max_batch_size']}")
        if p.cooldown_minutes < 0 or p.setup_minutes < 0:
            raise HTTPException(status_code=400, detail="minutes cannot be negative")
        mode = p.resource_mode.upper()
        if mode not in ("AUTO", "MACHINE", "MANUAL"):
            raise HTTPException(status_code=400, detail="resource_mode must be AUTO, MACHINE or MANUAL")
        row = db.query(StageScheduleConfig).filter(StageScheduleConfig.stage_id == p.stage_id).first()
        if not row:
            row = StageScheduleConfig(stage_id=p.stage_id)
            db.add(row)
        row.batch_size, row.next_day_release = p.batch_size, p.next_day_release
        row.cooldown_minutes, row.cooldown_working_time_only = p.cooldown_minutes, p.cooldown_working_time_only
        row.setup_minutes, row.resource_mode, row.needs_operator = p.setup_minutes, mode, p.needs_operator
        db.commit()
        return {"saved": p.stage_id}

    @router.delete("/schedule/config/{stage_id}")
    def reset_config(stage_id: int, db: Session = Depends(init_db)):
        db.query(StageScheduleConfig).filter(StageScheduleConfig.stage_id == stage_id).delete()
        db.commit()
        return {"reset": stage_id}

    # ---------------- shifts (change them at runtime; the next refresh re-plans) ----------------
    def _check_hm(v: Optional[str], label: str):
        try:
            _to_min(v)
        except Exception:
            raise HTTPException(status_code=400, detail=f"{label} must be HH:MM")

    @router.get("/schedule/shifts")
    def get_shifts(db: Session = Depends(init_db)):
        shifts, derived = _load_shifts(db, db.query(StageCapacityDB).all())
        rows = db.query(ShiftDefinition).order_by(ShiftDefinition.sort_order, ShiftDefinition.id).all()
        by_name = {r.name: r for r in rows}
        op_map: Dict[str, List[str]] = defaultdict(list)
        for a in db.query(OperatorShiftAssignment).all():
            op_map[a.operator_staff_no].append(a.shift_name)
        return {
            "derived": derived,
            "shifts": [{"id": by_name[s.name].id if s.name in by_name else None, "name": s.name,
                        "start_time": _hm(s.start_min), "end_time": _hm(s.end_min), "is_active": s.active}
                       for s in shifts],
            "changes": [{"id": c.id, "date_from": c.date_from.isoformat(),
                         "date_to": (c.date_to or c.date_from).isoformat(), "action": c.action,
                         "shift_name": c.shift_name, "start_time": c.start_time, "end_time": c.end_time,
                         "operator_staff_no": c.operator_staff_no, "note": c.note}
                        for c in db.query(ShiftAdjustment).order_by(ShiftAdjustment.date_from, ShiftAdjustment.id).all()],
            "operator_shifts": dict(op_map),
        }

    @router.post("/schedule/shifts/seed")
    def seed_shifts(db: Session = Depends(init_db)):
        """Store the shifts derived from stage_capacity as editable shift_definition rows."""
        if db.query(ShiftDefinition).count():
            return {"seeded": 0}
        shifts = _legacy_shifts(db.query(StageCapacityDB).all())
        for i, s in enumerate(shifts):
            db.add(ShiftDefinition(name=s.name, start_time=_hm(s.start_min), end_time=_hm(s.end_min),
                                   is_active=True, sort_order=i))
        db.commit()
        return {"seeded": len(shifts)}

    @router.post("/schedule/shifts")
    def save_shift(p: ShiftPayload, db: Session = Depends(init_db)):
        _check_hm(p.start_time, "start_time")
        _check_hm(p.end_time, "end_time")
        if _to_min(p.start_time) == _to_min(p.end_time):
            raise HTTPException(status_code=400, detail="start and end cannot be the same")
        row = db.query(ShiftDefinition).filter(ShiftDefinition.id == p.id).first() if p.id else None
        if not row:
            if db.query(ShiftDefinition).filter(ShiftDefinition.name == p.name).first():
                raise HTTPException(status_code=400, detail="A shift with this name already exists")
            row = ShiftDefinition()
            db.add(row)
        row.name, row.start_time, row.end_time = p.name.strip(), p.start_time, p.end_time
        row.is_active, row.sort_order = p.is_active, p.sort_order
        db.commit()
        return {"saved": row.id}

    @router.delete("/schedule/shifts/{shift_id}")
    def delete_shift(shift_id: int, db: Session = Depends(init_db)):
        db.query(ShiftDefinition).filter(ShiftDefinition.id == shift_id).delete()
        db.commit()
        return {"deleted": shift_id}

    @router.post("/schedule/shift-changes")
    def add_shift_change(p: ShiftChangePayload, db: Session = Depends(init_db)):
        action = p.action.lower()
        if action not in ("disable", "enable", "add"):
            raise HTTPException(status_code=400, detail="action must be disable, enable or add")
        if p.date_to and p.date_to < p.date_from:
            raise HTTPException(status_code=400, detail="date_to is before date_from")
        if action == "add":
            _check_hm(p.start_time, "start_time")
            _check_hm(p.end_time, "end_time")
        elif action == "enable" and not p.shift_name:
            raise HTTPException(status_code=400, detail="choose the shift to enable")
        db.add(ShiftAdjustment(date_from=p.date_from, date_to=p.date_to or p.date_from, action=action,
                               shift_name=p.shift_name, start_time=p.start_time, end_time=p.end_time,
                               operator_staff_no=_norm_staff(p.operator_staff_no), note=p.note))
        db.commit()
        return {"saved": True}

    @router.delete("/schedule/shift-changes/{change_id}")
    def delete_shift_change(change_id: int, db: Session = Depends(init_db)):
        db.query(ShiftAdjustment).filter(ShiftAdjustment.id == change_id).delete()
        db.commit()
        return {"deleted": change_id}

    @router.put("/schedule/operator-shifts/{staff_no}")
    def set_operator_shifts(staff_no: str, p: OperatorShiftsPayload, db: Session = Depends(init_db)):
        """Shifts one operator works (empty list = all active shifts)."""
        db.query(OperatorShiftAssignment).filter(OperatorShiftAssignment.operator_staff_no == staff_no).delete()
        for name in dict.fromkeys(p.shifts):
            db.add(OperatorShiftAssignment(operator_staff_no=staff_no, shift_name=name))
        db.commit()
        return {"saved": staff_no, "shifts": p.shifts}

    return router


# ==========================================================================================
# PART 3 - STANDALONE APP (port 2000)
# ==========================================================================================
SERVICE_PORT = int(os.getenv("SCHEDULE_SERVICE_PORT", "2000").strip() or 2000)


def init_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _make_verify_token():
    """Same rule as Data_Server: only checked when CHECK_JWT_VALIDATION == "True"."""
    if os.getenv("CHECK_JWT_VALIDATION", "False") != "True":
        return None
    from jose import JWTError, jwt                  # only needed when validation is switched on
    secret = os.getenv("JWT_SECRET")
    algorithm = os.getenv("ALGORITHM", "HS256")
    if not secret:
        raise ValueError("CHECK_JWT_VALIDATION is True but JWT_SECRET is not set")

    def verify_token(x_access_token: Optional[str] = Header(None, alias="x-access-token")):
        if not x_access_token:
            raise HTTPException(status_code=401, detail="Access Denied!")
        try:
            return jwt.decode(x_access_token, secret, algorithms=[algorithm])
        except JWTError:
            raise HTTPException(status_code=401, detail="Unauthorized! Token is invalid or expired.")
    return verify_token


app = FastAPI(title="SMT Schedule Service", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

try:
    base.metadata.create_all(db_engine)            # creates only missing tables (shift_definition, ...)
except Exception as exc:                           # never stop the service for this
    print(f"[schedule-service] create_all skipped: {exc}")

app.include_router(create_schedule_router(init_db, _make_verify_token()))


@app.get("/schedule/health")
def health():
    return {"service": "smt-schedule", "status": "ok", "port": SERVICE_PORT,
            "time": datetime.now().isoformat(timespec="seconds")}


if __name__ == "__main__":
    import uvicorn
    print(f"[schedule-service] starting on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)