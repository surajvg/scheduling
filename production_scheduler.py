"""
production_scheduler.py
=======================
Pure scheduling logic for the production schedule. No database and no FastAPI in this file.

    Data_Server.py  -> load_schedule_inputs(db)  reads the tables into plain dicts
                    -> make_schedule(inputs)     (this file) returns the JSON for GET /schedule/data
                    -> validate_changes(...)     (this file) checks the changes of POST /schedule/update

Run `python production_scheduler.py` to test the scheduler on hard-coded sample data (no database needed).

Words used in this file
    operation  = one (production order, process step). All PCBs of a PO move together (lot based).
    booking    = a time range (start, end) during which a machine or an operator is taken.
    row        = one scheduled operation (internal dict, turned into JSON at the end).
All times are naive local datetimes; in JSON they are "YYYY-MM-DDTHH:MM:SS".
"""
import math
from datetime import date, datetime, time, timedelta

ISO = "%Y-%m-%dT%H:%M:%S"
MAX_DAYS_SEARCH = 365          # stop looking for a working day after this many days (no infinite loop)
MAX_WORKING_DAYS_PER_OP = 5000  # an operation longer than this is a data error (wrong process time)

# Used for every stage that has no row in process_schedule_config (A4)
DEFAULT_STAGE_CONFIG = {
    "batch_size": 30,                    # BATCH steps: PCBs processed together in one run
    "next_day_release": True,            # BATCH steps: machine is free again only next working day
    "cooldown_minutes": 0.0,             # wait after the step before the NEXT step of the PO may start
    "cooldown_working_time_only": False, # False = cooldown also runs at night / on holidays
    "setup_minutes": 0.0,                # added before every operation
}


def iso(dt):
    """datetime -> 'YYYY-MM-DDTHH:MM:SS' (None stays None)."""
    return dt.strftime(ISO) if dt else None


def parse_iso(text):
    """'YYYY-MM-DDTHH:MM[:SS]' -> datetime. Returns None when empty or not a valid date."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).strip()).replace(tzinfo=None, microsecond=0)
    except ValueError:
        return None


def saturday_number(d):
    """1 for the 1st Saturday of the month, 2 for the 2nd, ..."""
    return (d.day - 1) // 7 + 1


# ======================================================================================
# A2. WORKING CALENDAR
# ======================================================================================
class Calendar:
    """
    public_holidays : {date: "Diwali", ...}                                  (public_holidays table)
    exceptions      : {date: ("working" | "half-day" | "holiday", note)}     (calendar_exceptions table)
    One working window per day: shift_start_hour .. shift_start_hour + work_hours.
    A half day uses only the first half of that window.
    """

    def __init__(self, public_holidays, exceptions, shift_start_hour=6, work_hours=14, shifts_per_day=2):
        self.public_holidays = public_holidays
        self.exceptions = exceptions
        self.shift_start_hour = float(shift_start_hour)
        self.work_hours = float(work_hours)
        self.shifts_per_day = max(int(shifts_per_day), 1)
        self._windows = {}              # cache: date -> (window start, window end)

    def day_factor(self, d):
        """1.0 = full working day, 0.5 = half day, 0.0 = no work. Rules are checked in this order."""
        if d in self.exceptions:                                  # 1. calendar_exceptions always wins
            kind = self.exceptions[d][0]
            if kind == "working":
                return 1.0
            if kind == "half-day":
                return 0.5
            if kind == "holiday":
                return 0.0
        if d in self.public_holidays:                             # 2. public holiday
            return 0.0
        if d.weekday() == 6:                                      # 3. every Sunday
            return 0.0
        if d.weekday() == 5 and saturday_number(d) in (1, 3):     # 4. 1st and 3rd Saturday of every month
            return 0.0
        return 1.0                                                # 5. normal working day

    def off_reason(self, d):
        """Why a day is not working (for the shaded holidays in the chart)."""
        if d in self.exceptions and self.exceptions[d][0] == "holiday":
            return self.exceptions[d][1] or "Holiday"
        if d in self.public_holidays:
            return self.public_holidays[d] or "Public holiday"
        if d.weekday() == 6:
            return "Sunday"
        return "%s Saturday off" % ("1st" if saturday_number(d) == 1 else "3rd")

    def window(self, d):
        """(start, end) of the working window on day d. start == end on a day off."""
        if d not in self._windows:      # remember every day once calculated (called very often)
            start = datetime.combine(d, time()) + timedelta(hours=self.shift_start_hour)
            self._windows[d] = (start, start + timedelta(hours=self.work_hours * self.day_factor(d)))
        return self._windows[d]

    def is_working_time(self, dt):
        start, end = self.window(dt.date())
        return start <= dt < end

    def next_working_start(self, dt):
        """Earliest moment >= dt that lies inside a working window."""
        d = dt.date()
        for _ in range(MAX_DAYS_SEARCH + 1):
            start, end = self.window(d)
            if end > start:                 # it is a working day
                if dt < start:
                    return start
                if dt < end:
                    return dt
            d += timedelta(days=1)
        raise RuntimeError("No working day in the %d days after %s - check public_holidays and "
                           "calendar_exceptions." % (MAX_DAYS_SEARCH, dt.strftime("%Y-%m-%d")))

    def next_day_start(self, dt):
        """Start of the first working window on a LATER day than dt."""
        return self.next_working_start(datetime.combine(dt.date() + timedelta(days=1), time()))

    def add_working_minutes(self, start, minutes):
        """Walk `minutes` of working time forward from start. Work that does not fit today continues
        at the start of the next working window (the operation is split across days)."""
        current = self.next_working_start(start)
        left = float(minutes)
        for _ in range(MAX_WORKING_DAYS_PER_OP):
            day_end = self.window(current.date())[1]
            room = (day_end - current).total_seconds() / 60.0
            if left <= room:
                return current + timedelta(seconds=round(left * 60))
            left -= room
            current = self.next_day_start(current)
        raise RuntimeError("An operation needs more than %d working days - check the process times."
                           % MAX_WORKING_DAYS_PER_OP)

    def shifts(self):
        """[{"name": "Shift 1", "start": "06:00", "end": "13:00"}, ...] for the JSON."""
        per_shift = self.work_hours / self.shifts_per_day
        result = []
        for k in range(self.shifts_per_day):
            start = self.shift_start_hour + k * per_shift
            result.append({"name": "Shift %d" % (k + 1), "start": hhmm(start), "end": hhmm(start + per_shift)})
        return result


def hhmm(hours):
    """6.5 -> '06:30'"""
    h = int(hours)
    m = int(round((hours - h) * 60))
    return "%02d:%02d" % (h, m)


# ======================================================================================
# A3. PLACING ONE OPERATION ON THE CALENDAR
# ======================================================================================
def stage_cfg(config_by_stage, stage_id):
    """Config of a stage = defaults overwritten by its process_schedule_config row (if any)."""
    cfg = dict(DEFAULT_STAGE_CONFIG)
    cfg.update(config_by_stage.get(stage_id) or {})
    return cfg


def batch_count(qty, cfg):
    """R3: number of batch runs. A partial last batch still counts as a full batch."""
    return max(1, math.ceil(qty / max(int(cfg["batch_size"]), 1)))


def machine_free_after(cal, op, cfg, end):
    """When the machine can take the next operation after this one ends (R3 next-day release)."""
    if op["process_mode"] == "BATCH" and cfg["next_day_release"]:
        return cal.next_day_start(end)
    return end


def ready_after(cal, end, cfg):
    """R5: when the NEXT step of the same PO may start = end + cooldown of this step."""
    cooldown = cfg["cooldown_minutes"]
    if cooldown <= 0:
        return end
    if cfg["cooldown_working_time_only"]:
        return cal.add_working_minutes(end, cooldown)
    return end + timedelta(minutes=cooldown)     # clock time: also runs at night and on holidays


def place_operation(cal, op, qty, cfg, earliest, with_setup=True):
    """
    Put `qty` PCBs of one operation on the calendar, starting at `earliest` or later.
    Ignores other operations (find_slot() takes care of those).
    Returns {"start", "end", "machine_free_at", "runs": [(start, end), ...]}
      runs = the times somebody actually works on it (the operator is booked only for these).
    """
    start = cal.next_working_start(earliest)
    t = start
    if with_setup and cfg["setup_minutes"] > 0:
        t = cal.add_working_minutes(t, cfg["setup_minutes"])

    if op["process_mode"] == "SERIES":
        # R2: SERIES - PCBs one after the other, duration = qty x minutes_per_pcb
        end = cal.add_working_minutes(t, qty * op["minutes_per_pcb"])
        return {"start": start, "end": end, "machine_free_at": end, "runs": [(start, end)]}

    # R3: BATCH - ceil(qty / batch_size) runs, every run takes minutes_per_batch
    runs = []
    run_start = start
    for _ in range(batch_count(qty, cfg)):
        run_end = cal.add_working_minutes(t, op["minutes_per_batch"])
        runs.append((run_start, run_end))
        t = machine_free_after(cal, op, cfg, run_end)   # next run: next working day (or right away)
        run_start = cal.next_working_start(t)
    end = runs[-1][1]
    return {"start": start, "end": end, "machine_free_at": t, "runs": runs}


def clash(start, end, bookings):
    """Latest end of the bookings that overlap [start, end). None when nothing overlaps."""
    latest = None
    for b_start, b_end in bookings:
        if b_start < end and start < b_end:          # the two time ranges overlap
            if latest is None or b_end > latest:
                latest = b_end
    return latest


def find_slot(cal, op, qty, cfg, earliest, machine_bookings, operator_bookings, with_setup=True):
    """
    Earliest placement at or after `earliest` that overlaps neither the machine's bookings
    (machine is held from start until machine_free_at) nor the operator's bookings (only the runs).
    Every time something is in the way we try again right after it, so this always ends.
    Returns (slot, blocked_by) - blocked_by = "machine busy" / "operator busy" / "".
    """
    t = earliest
    blocked_by = ""
    for _ in range(len(machine_bookings) + len(operator_bookings) + 2):
        slot = place_operation(cal, op, qty, cfg, t, with_setup)

        machine_clash = clash(slot["start"], slot["machine_free_at"], machine_bookings)
        if machine_clash:
            t, blocked_by = machine_clash, "machine busy"
            continue

        operator_clash = None
        for run_start, run_end in slot["runs"]:
            operator_clash = clash(run_start, run_end, operator_bookings)
            if operator_clash:
                break
        if operator_clash:
            t, blocked_by = operator_clash, "operator busy"
            continue

        return slot, blocked_by
    raise RuntimeError("No free slot found for operation %s." % op["op_id"])


# ======================================================================================
# A3. THE SCHEDULER (forward, finite capacity)
# ======================================================================================
def run_scheduler(cal, orders, sequence, machines_by_stage, operators_by_stage, config_by_stage,
                  frozen=None, not_before=None, use_progress=False):
    """
    orders     : {po_no: {"release": datetime, "ops": [op, ...] in routing order}}
    sequence   : po numbers in priority order (R7) - the first PO gets the resources first
    frozen     : {op_id: row} operations that run / are finished; they keep their times (R8)
    not_before : nothing new is scheduled before this moment (now). None for the planned schedule.
    use_progress : True for the current schedule - a running operation is scheduled only for the
                   PCBs still to do (no setup), like any other operation, so it can never overlap.
    Returns {op_id: {"machine_id", "operator_name", "start", "end", "machine_free_at", "runs", "waiting_reason"}}
    """
    frozen = frozen or {}
    machine_bookings = {}    # machine_id    -> [(start, machine_free_at), ...]
    operator_bookings = {}   # operator_name -> [(run_start, run_end), ...]
    result = {}

    # Step 1: frozen operations are booked first, nobody else may use that time
    for fx in frozen.values():
        machine_bookings.setdefault(fx["machine_id"], []).append((fx["start"], fx["machine_free_at"]))
        if fx["operator_name"]:
            operator_bookings.setdefault(fx["operator_name"], []).extend(fx["runs"])

    # Step 2: POs in priority order, steps of a PO in routing order
    for po_no in sequence:
        po = orders[po_no]
        earliest = po["release"]
        if not_before and not_before > earliest:
            earliest = not_before
        prev_end = None      # end of the previous step of this PO
        prev_ready = None    # prev_end + cooldown of the previous step

        for op in po["ops"]:
            stage_id = op["flow_step_id"]
            cfg = stage_cfg(config_by_stage, stage_id)

            if op["op_id"] in frozen:
                fx = frozen[op["op_id"]]
                result[op["op_id"]] = fx
                prev_end, prev_ready = fx["end"], ready_after(cal, fx["end"], cfg)
                continue

            # R1 + R5: not before the previous step (+ its cooldown) is completely finished
            ready = earliest
            if prev_ready and prev_ready > ready:
                ready = prev_ready

            # R8: a running operation only needs the time for the PCBs that are still to do
            qty, with_setup = op["quantity"], True
            if use_progress and op_status(op) == "IN_PROGRESS":
                qty, with_setup = op["quantity"] - op["completed_qty"], False

            # R4 + R6: try every machine of the stage with every qualified operator,
            #          keep the pair that FINISHES earliest ("" = no qualified operator)
            operators = operators_by_stage.get(stage_id) or [""]
            best = None
            for machine in machines_by_stage[stage_id]:
                for operator in operators:
                    slot, blocked_by = find_slot(
                        cal, op, qty, cfg, ready,
                        machine_bookings.get(machine["machine_id"], []),
                        operator_bookings.get(operator, []) if operator else [], with_setup)
                    if best is None or slot["end"] < best["end"]:
                        best = dict(slot, machine_id=machine["machine_id"], operator_name=operator,
                                    blocked_by=blocked_by)

            # why does it start later than the PO could start? (only shown in the tooltip)
            if best["blocked_by"]:
                reason = best["blocked_by"]
            elif prev_ready and prev_ready > earliest:
                reason = "cooldown" if prev_ready > prev_end else "previous step"
            else:
                reason = ""

            machine_bookings.setdefault(best["machine_id"], []).append((best["start"], best["machine_free_at"]))
            if best["operator_name"]:
                operator_bookings.setdefault(best["operator_name"], []).extend(best["runs"])

            result[op["op_id"]] = {
                "machine_id": best["machine_id"], "operator_name": best["operator_name"],
                "start": best["start"], "end": best["end"], "machine_free_at": best["machine_free_at"],
                "runs": best["runs"], "waiting_reason": reason,
            }
            prev_end, prev_ready = best["end"], ready_after(cal, best["end"], cfg)
    return result


def op_status(op):
    """COMPLETED / IN_PROGRESS / SCHEDULED from the OperatorLog progress."""
    if op["completed_qty"] >= op["quantity"]:
        return "COMPLETED"
    if op["completed_qty"] > 0 or op["started"]:
        return "IN_PROGRESS"
    return "SCHEDULED"


# ======================================================================================
# R9 / A5. CHECK ONE MANUAL CHANGE
# ======================================================================================
def check_change(state, row, machine_id, start, end=None):
    """
    Validate moving `row` to machine_id / start (/ end). Uses the current schedule in state["rows"].
    Returns {"reason": "" when OK, else why not, "slot": new times, "operator_name": operator to use}
    """
    cal, now, op, cfg = state["cal"], state["now"], row["op"], row["cfg"]
    rows = state["rows"]

    def fail(reason):
        return {"reason": reason, "slot": None, "operator_name": None}

    if row["status"] == "COMPLETED":
        return fail("operation is already completed")
    machine = state["machines_by_id"].get(machine_id)
    if machine is None:
        return fail("machine %s does not exist (deleted?)" % machine_id)
    if machine["stage_id"] != op["flow_step_id"]:
        return fail("machine %s does not belong to stage '%s'" % (machine_id, op["step_name"]))
    if start < now:
        return fail("start %s is in the past" % iso(start))
    if not cal.is_working_time(start):
        return fail("start %s is not working time (Sunday, 1st/3rd Saturday, holiday or outside the shift)"
                    % iso(start))

    # new times: end from the calendar unless it was given
    if end is None:
        slot = place_operation(cal, op, op["quantity"] - op["completed_qty"], cfg, start)
    elif end <= start:
        return fail("end must be after start")
    else:
        slot = {"start": start, "end": end, "machine_free_at": machine_free_after(cal, op, cfg, end),
                "runs": [(start, end)]}

    # previous / next step of the same PO
    same_po = [r for r in rows if r["op"]["production_order"] == op["production_order"]]
    same_po.sort(key=lambda r: r["op"]["operation_no"])
    i = same_po.index(row)
    if i > 0:
        prev = same_po[i - 1]
        ready = ready_after(cal, prev["end"], prev["cfg"])
        if start < ready:
            return fail("start is before the previous step %s is finished%s (%s)"
                        % (prev["op"]["op_id"], " + cooldown" if ready > prev["end"] else "", iso(ready)))
    if i < len(same_po) - 1:
        nxt = same_po[i + 1]
        my_ready = ready_after(cal, slot["end"], cfg)
        if my_ready > nxt["start"]:
            return fail("it would end%s at %s, after the next step %s starts (%s)"
                        % (" + cooldown" if my_ready > slot["end"] else "", iso(my_ready),
                           nxt["op"]["op_id"], iso(nxt["start"])))

    # no overlap with another operation on the same machine
    for other in rows:
        if other is not row and other["machine_id"] == machine_id:
            if other["start"] < slot["machine_free_at"] and slot["start"] < other["machine_free_at"]:
                return fail("machine %s is busy with %s from %s to %s" % (
                    machine_id, other["op"]["op_id"], iso(other["start"]), iso(other["machine_free_at"])))

    # operator: keep the current one if free, otherwise any other qualified operator who is free
    candidates = [row["operator_name"]] if row["operator_name"] else []
    candidates += [n for n in state["operators_by_stage"].get(op["flow_step_id"], []) if n not in candidates]
    operator = ""
    if candidates:
        operator = None
        for name in candidates:
            bookings = []
            for other in rows:
                if other is not row and other["operator_name"] == name:
                    bookings.extend(other["runs"])
            if all(clash(s, e, bookings) is None for s, e in slot["runs"]):
                operator = name
                break
        if operator is None:
            return fail("no qualified operator is free at that time (%s)" % ", ".join(candidates))

    return {"reason": "", "slot": slot, "operator_name": operator}


def apply_change(row, machine_id, check):
    """Move the row to the checked position."""
    row.update(check["slot"])
    row["machine_id"] = machine_id
    row["operator_name"] = check["operator_name"]
    row["waiting_reason"] = ""


def validate_changes(state, changes):
    """
    POST /schedule/update: check every change BEFORE saving. Valid changes are applied to the
    in-memory schedule right away, so the next change in the same request is checked against them.
    changes: [{"op_id", "machine_id", "start_time", "end_time"}]
    Returns [{"op_id", "ok", "reason", "machine_id", "start_time", "end_time"}]
    """
    rows_by_id = {r["op"]["op_id"]: r for r in state["rows"]}
    results = []
    for ch in changes:
        op_id = ch.get("op_id")
        row = rows_by_id.get(op_id)
        if row is None:
            results.append({"op_id": op_id, "ok": False, "reason": "operation does not exist in the schedule"})
            continue
        if ch.get("start_time") and parse_iso(ch["start_time"]) is None:
            results.append({"op_id": op_id, "ok": False, "reason": "start_time must be YYYY-MM-DDTHH:MM:SS"})
            continue
        if ch.get("end_time") and parse_iso(ch["end_time"]) is None:
            results.append({"op_id": op_id, "ok": False, "reason": "end_time must be YYYY-MM-DDTHH:MM:SS"})
            continue
        machine_id = ch.get("machine_id") or row["machine_id"]
        start = parse_iso(ch.get("start_time")) or row["start"]
        end = parse_iso(ch.get("end_time"))
        check = check_change(state, row, machine_id, start, end)
        if check["reason"]:
            results.append({"op_id": op_id, "ok": False, "reason": check["reason"]})
            continue
        apply_change(row, machine_id, check)
        results.append({"op_id": op_id, "ok": True, "reason": "", "machine_id": machine_id,
                        "start_time": iso(start), "end_time": iso(end)})   # end_time None = calculated
    return results


# ======================================================================================
# BUILD THE WHOLE SCHEDULE
# ======================================================================================
def build_state(inp):
    """
    inp = output of load_schedule_inputs() in Data_Server.py (or the sample dict at the bottom):
      now, calendar, orders, sequence, machines, operators_by_stage, stage_config,
      stage_names, stage_order, overrides, warnings
    Returns the schedule as internal rows + everything needed to check changes / write JSON.
    """
    cal, now = inp["calendar"], inp["now"]
    orders, sequence = inp["orders"], inp["sequence"]
    config, operators_by_stage = inp["stage_config"], inp["operators_by_stage"]
    warnings = list(inp["warnings"])

    # ---- 1. machines per stage; virtual MANUAL station for a stage without machine (R4) ----
    machines = [dict(m) for m in inp["machines"]]
    machines_by_stage = {}
    for m in machines:
        machines_by_stage.setdefault(m["stage_id"], []).append(m)
    used_stages = []
    for po_no in sequence:
        for op in orders[po_no]["ops"]:
            if op["flow_step_id"] not in used_stages:
                used_stages.append(op["flow_step_id"])
    for stage_id in used_stages:
        name = inp["stage_names"].get(stage_id, "Step %s" % stage_id)
        if not machines_by_stage.get(stage_id):
            m = {"machine_id": "MANUAL-%s" % stage_id, "machine_name": "%s (manual)" % name,
                 "work_centre": name, "stage_id": stage_id, "resource_type": "MANUAL"}
            machines.append(m)
            machines_by_stage[stage_id] = [m]
            warnings.append("Stage '%s' has no machine in equipment_details - using virtual station MANUAL-%s."
                            % (name, stage_id))
        if not operators_by_stage.get(stage_id):
            warnings.append("Stage '%s' has no qualified operator in operator_step_mapping - scheduled "
                            "without operator." % name)
    machines_by_id = {m["machine_id"]: m for m in machines}

    # ---- 2. PLANNED schedule: release order only, no progress, no overrides (R10) ----
    plan = run_scheduler(cal, orders, sequence, machines_by_stage, operators_by_stage, config)

    # ---- 3. COMPLETED operations keep their real times (R8) ----
    override_by_op = {ov["op_id"]: ov for ov in inp["overrides"]}
    frozen = {}
    done_bookings = {}       # machine_id -> [(start, end)] of completed work, to spread it over the machines
    completed = [op for po_no in sequence for op in orders[po_no]["ops"] if op_status(op) == "COMPLETED"]
    completed.sort(key=lambda op: op["actual_start"] or op["actual_end"] or now)
    for op in completed:
        cfg = stage_cfg(config, op["flow_step_id"])
        planned = plan[op["op_id"]]
        end = op["actual_end"] or now
        start = min(op["actual_start"] or planned["start"], end)

        # OperatorLog has no machine column: machine saved from the UI (if valid), else the first machine
        # of the stage that was free at that time, else the planned one
        stage_machines = [m["machine_id"] for m in machines_by_stage[op["flow_step_id"]]]
        machine_id = planned["machine_id"]
        ov = override_by_op.get(op["op_id"])
        if ov and ov.get("machine_id") in stage_machines:
            machine_id = ov["machine_id"]
        else:
            for mid in stage_machines:
                if clash(start, end, done_bookings.get(mid, [])) is None:
                    machine_id = mid
                    break
        done_bookings.setdefault(machine_id, []).append((start, end))

        frozen[op["op_id"]] = {"machine_id": machine_id, "operator_name": op["actual_operator"] or planned["operator_name"],
                               "start": start, "end": end, "machine_free_at": machine_free_after(cal, op, cfg, end),
                               "runs": [(start, end)], "waiting_reason": ""}

    # ---- 4. CURRENT schedule: POs that already started go first (oldest start first), then the
    #         normal order. Running operations get the time for their remaining PCBs from now on,
    #         one after the other, so two orders never share a machine and step N+1 waits for step N.
    def first_actual_start(po_no):
        starts = [op["actual_start"] for op in orders[po_no]["ops"] if op_status(op) != "SCHEDULED" and op["actual_start"]]
        return min(starts) if starts else None

    started = [po_no for po_no in sequence
               if first_actual_start(po_no) and any(op_status(op) != "COMPLETED" for op in orders[po_no]["ops"])]
    started.sort(key=first_actual_start)
    current_sequence = started + [po_no for po_no in sequence if po_no not in started]

    current = run_scheduler(cal, orders, current_sequence, machines_by_stage, operators_by_stage, config,
                            frozen=frozen, not_before=now, use_progress=True)

    rows = []
    for po_no in sequence:
        for op in orders[po_no]["ops"]:
            row = dict(current[op["op_id"]])
            row.update({"op": op, "cfg": stage_cfg(config, op["flow_step_id"]), "status": op_status(op),
                        "plan": plan[op["op_id"]]})
            rows.append(row)

    state = {"cal": cal, "now": now, "rows": rows, "orders": orders, "sequence": sequence,
             "machines": machines, "machines_by_id": machines_by_id, "operators_by_stage": operators_by_stage,
             "stage_order": inp["stage_order"], "warnings": warnings}

    # ---- 5. manual changes from schedule_override, in the order they were saved (R9) ----
    rows_by_id = {r["op"]["op_id"]: r for r in rows}
    missing = 0
    for ov in inp["overrides"]:
        row = rows_by_id.get(ov["op_id"])
        if row is None:
            missing += 1
            continue
        if row["status"] == "COMPLETED":
            continue      # finished operations keep their real times (machine was used in step 3)
        start = parse_iso(ov.get("start_time")) or row["start"]
        end = parse_iso(ov.get("end_time"))
        machine_id = ov.get("machine_id") or row["machine_id"]
        check = check_change(state, row, machine_id, start, end)
        if check["reason"]:
            warnings.append("Saved change for %s ignored: %s." % (ov["op_id"], check["reason"]))
        else:
            apply_change(row, machine_id, check)
    if missing:
        warnings.append("%d saved schedule change(s) point to operations that are no longer in the schedule "
                        "and were ignored." % missing)
    return state


def calendar_json(cal, first_day, last_day):
    """Every non-working day between first_day and last_day (incl. Sundays and 1st/3rd Saturdays)."""
    holidays, half_days, working_exceptions = [], [], []
    d = first_day
    while d <= last_day:
        factor = cal.day_factor(d)
        if factor == 0:
            holidays.append({"holiday_date": d.isoformat(), "name": cal.off_reason(d)})
        elif factor == 0.5:
            half_days.append(d.isoformat())
        if d in cal.exceptions and cal.exceptions[d][0] == "working":
            working_exceptions.append({"date": d.isoformat(), "note": cal.exceptions[d][1] or ""})
        d += timedelta(days=1)
    shift_end = cal.shift_start_hour + cal.work_hours
    return {
        "shift_start": int(cal.shift_start_hour) if cal.shift_start_hour.is_integer() else cal.shift_start_hour,
        "shift_end": int(shift_end) if shift_end.is_integer() else shift_end,
        "shifts": cal.shifts(),
        "holidays": holidays,
        "half_days": half_days,
        "working_exceptions": working_exceptions,
    }


def row_json(cal, row):
    """One internal row -> one entry of "operations" (exact A5 shape)."""
    op, cfg, plan = row["op"], row["cfg"], row["plan"]
    is_batch = op["process_mode"] == "BATCH"
    cooldown_until = ready_after(cal, row["end"], cfg) if cfg["cooldown_minutes"] > 0 else None
    return {
        "op_id": op["op_id"],
        "production_order": op["production_order"],
        "part_number": op["part_number"],
        "operation_no": op["operation_no"],
        "flow_step_id": op["flow_step_id"],
        "step_name": op["step_name"],
        "operation_name": op["step_name"],      # same value; the old React UI reads this name
        "process_mode": op["process_mode"],
        "quantity": op["quantity"],
        "completed_qty": op["completed_qty"],
        "batch_size": int(cfg["batch_size"]) if is_batch else None,
        "batch_count": batch_count(op["quantity"], cfg) if is_batch else None,
        "cooldown_minutes": cfg["cooldown_minutes"],
        "cooldown_until": iso(cooldown_until),
        "machine_id": row["machine_id"],
        "operator_name": row["operator_name"],
        "start_time": iso(row["start"]),
        "end_time": iso(row["end"]),
        "planned_machine_id": plan["machine_id"],
        "planned_start": iso(plan["start"]),
        "planned_end": iso(plan["end"]),
        "actual_start": iso(op["actual_start"]),
        "actual_end": iso(op["actual_end"]) if row["status"] == "COMPLETED" else None,
        "status": row["status"],
        "waiting_reason": row["waiting_reason"],
    }


def make_schedule(inp):
    """Everything GET /schedule/data returns (A5)."""
    state = build_state(inp)
    cal, now, rows = state["cal"], state["now"], state["rows"]

    operations = [row_json(cal, r) for r in rows]
    operations.sort(key=lambda o: (o["start_time"], o["production_order"], o["operation_no"]))

    production_orders = []
    for po_no in state["sequence"]:
        po = state["orders"][po_no]
        po_rows = [r for r in rows if r["op"]["production_order"] == po_no]
        statuses = [r["status"] for r in po_rows]
        if all(s == "COMPLETED" for s in statuses):
            status = "COMPLETED"
        elif any(s != "SCHEDULED" for s in statuses):
            status = "IN_PROGRESS"
        else:
            status = "SCHEDULED"
        production_orders.append({
            "production_order": po_no, "part_number": po["part_number"], "description": po["description"],
            "quantity": po["quantity"], "release_date": iso(po["release"]),
            "scheduled_start": iso(min(r["start"] for r in po_rows)),
            "completion_date": iso(max(r["end"] for r in po_rows)),
            "status": status,
        })

    # machines sorted by the step order of their stage
    machines = [{"machine_id": m["machine_id"], "machine_name": m["machine_name"], "work_centre": m["work_centre"],
                 "stage_id": m["stage_id"], "resource_type": m["resource_type"]} for m in state["machines"]]
    machines.sort(key=lambda m: (state["stage_order"].get(m["stage_id"], 9999), str(m["machine_id"])))

    operators = {}
    for stage_id, names in state["operators_by_stage"].items():
        for name in names:
            operators.setdefault(name, []).append(stage_id)

    # shaded days: from 30 days ago (or the oldest bar) to 180 days ahead (or the last bar + 7 days)
    first_day = now.date() - timedelta(days=30)
    last_day = now.date() + timedelta(days=180)
    for r in rows:
        first_day = min(first_day, r["start"].date(), r["plan"]["start"].date())
        last_day = max(last_day, r["end"].date() + timedelta(days=7), r["plan"]["end"].date() + timedelta(days=7))

    return {
        "generated_at": iso(now),
        "calendar": calendar_json(cal, first_day, last_day),
        "machines": machines,
        "operators": [{"operator_name": n, "stages": s} for n, s in sorted(operators.items())],
        "production_orders": production_orders,
        "operations": operations,
        "warnings": list(dict.fromkeys(state["warnings"])),   # drop duplicates, keep order
    }


# ======================================================================================
# A7. TEST WITHOUT DATABASE:  python production_scheduler.py
# ======================================================================================
if __name__ == "__main__":
    D = datetime
    # October 2026: Thu 1 = working, Fri 2 = public holiday, Sat 3 = 1st Saturday (off),
    # Sun 4 = Sunday (off), Mon 5 = Half-Day exception, Sun 11 = Sunday marked 'Working'
    cal = Calendar(
        public_holidays={date(2026, 10, 2): "Gandhi Jayanti"},
        exceptions={date(2026, 10, 5): ("half-day", "Stock taking"),
                    date(2026, 10, 11): ("working", "Extra Sunday shift")},
        shift_start_hour=6, work_hours=14, shifts_per_day=2,      # 06:00 - 20:00
    )

    # 5-step routing. Step 3 is a BATCH oven: 6 PCBs per run, 120 min per run, 10 h cooldown after it.
    routing = [  # (stage_id, name, mode, minutes per PCB (SERIES) or per batch (BATCH))
        (1, "SMT", "SERIES", 1),
        (2, "AOI", "SERIES", 30),
        (3, "Curing oven", "BATCH", 120),
        (4, "Varnish", "SERIES", 60),
        (5, "Final inspection", "SERIES", 5),
    ]

    def sample_po(po_no, release):
        ops = []
        for seq, (stage_id, name, mode, minutes) in enumerate(routing, start=1):
            ops.append({
                "op_id": "%s-%d" % (po_no, seq * 10), "production_order": po_no, "part_number": "PN-100",
                "operation_no": seq * 10, "flow_step_id": stage_id, "step_name": name, "process_mode": mode,
                "quantity": 10,
                "minutes_per_pcb": minutes if mode == "SERIES" else None,
                "minutes_per_batch": minutes if mode == "BATCH" else None,
                "completed_qty": 0, "started": False,
                "actual_start": None, "actual_end": None, "actual_operator": "",
            })
        return {"production_order": po_no, "part_number": "PN-100", "description": "Sample board",
                "quantity": 10, "release": release, "ops": ops}

    orders = {"PO-1": sample_po("PO-1", D(2026, 10, 1, 6, 0)),
              "PO-2": sample_po("PO-2", D(2026, 10, 1, 6, 0))}
    sample = {
        "now": D(2026, 10, 1, 6, 0),
        "calendar": cal,
        "orders": orders,
        "sequence": ["PO-1", "PO-2"],
        "machines": [   # stage 5 has no machine -> MANUAL-5 + warning
            {"machine_id": "SMT-1", "machine_name": "SMT line", "work_centre": "SMT", "stage_id": 1, "resource_type": "MACHINE"},
            {"machine_id": "AOI-1", "machine_name": "AOI", "work_centre": "AOI", "stage_id": 2, "resource_type": "MACHINE"},
            {"machine_id": "OVEN-1", "machine_name": "Curing oven", "work_centre": "Oven", "stage_id": 3, "resource_type": "MACHINE"},
            {"machine_id": "VAR-1", "machine_name": "Varnish booth", "work_centre": "Varnish", "stage_id": 4, "resource_type": "MACHINE"},
        ],
        "operators_by_stage": {1: ["Ravi"], 2: ["Asha", "Kiran"], 3: ["Ravi"], 4: ["Meena"]},  # stage 5: none
        "stage_config": {3: {"batch_size": 6, "next_day_release": True, "cooldown_minutes": 600,
                             "cooldown_working_time_only": False, "setup_minutes": 0}},
        "stage_names": {sid: name for sid, name, _, _ in routing},
        "stage_order": {sid: sid for sid, _, _, _ in routing},
        "overrides": [],
        "warnings": [],
    }

    out = make_schedule(sample)

    def show(t):
        return D.fromisoformat(t).strftime("%a %d %H:%M") if t else ""

    print("%-9s %-17s %-6s %-9s %-6s %-15s %-15s %-15s %s"
          % ("Op", "Step", "Mode", "Machine", "Oper.", "Start", "End", "Cooldown until", "Waiting"))
    for o in sorted(out["operations"], key=lambda o: (o["production_order"], o["operation_no"])):
        print("%-9s %-17s %-6s %-9s %-6s %-15s %-15s %-15s %s" % (
            o["op_id"], o["step_name"], o["process_mode"], o["machine_id"], o["operator_name"] or "-",
            show(o["start_time"]), show(o["end_time"]), show(o["cooldown_until"]), o["waiting_reason"]))
    print("\nNon-working days:", ", ".join("%s (%s)" % (h["holiday_date"], h["name"])
                                          for h in out["calendar"]["holidays"] if h["holiday_date"] < "2026-10-12"
                                          and h["holiday_date"] >= "2026-10-01"))
    print("Half days:", out["calendar"]["half_days"])
    print("Warnings:")
    for w in out["warnings"]:
        print("  -", w)

    # ---- checks of the rules ----
    ops = {o["op_id"]: o for o in out["operations"]}
    assert ops["PO-1-10"]["end_time"] == "2026-10-01T06:10:00"         # 10 PCB x 1 min
    assert ops["PO-1-20"]["start_time"] == ops["PO-1-10"]["end_time"]   # R1 step 2 after step 1
    assert ops["PO-2-10"]["start_time"] == ops["PO-1-10"]["end_time"]   # R2 PO-2 gets SMT as soon as PO-1 frees it
    assert ops["PO-1-30"]["batch_count"] == 2                           # R3 10 PCBs / 6 per batch = 2 runs
    assert ops["PO-1-30"]["end_time"] == "2026-10-05T08:00:00"          # 2nd run on the next working day (Mon 5)
    assert ops["PO-1-40"]["start_time"] >= ops["PO-1-30"]["cooldown_until"]   # R5 cooldown
    for o in out["operations"]:                                          # nothing outside working time
        assert cal.is_working_time(D.fromisoformat(o["start_time"])), o["op_id"]

    # ---- POST /schedule/update checks (A5) ----
    state = build_state(sample)
    results = validate_changes(state, [
        {"op_id": "PO-2-50", "machine_id": "MANUAL-5", "start_time": "2026-10-11T10:00:00"},  # Sunday marked Working
        {"op_id": "PO-2-50", "machine_id": "MANUAL-5", "start_time": "2026-10-18T10:00:00"},  # normal Sunday
        {"op_id": "PO-2-50", "machine_id": "SMT-1", "start_time": "2026-10-12T10:00:00"},     # wrong stage
        {"op_id": "PO-1-20", "machine_id": "AOI-1", "start_time": "2026-10-01T06:05:00"},     # before step 1 ends
    ])
    print("\nValidation of manual changes:")
    for r in results:
        print("  %-8s %s" % (r["op_id"], "OK" if r["ok"] else "REJECTED: " + r["reason"]))
    assert [r["ok"] for r in results] == [True, False, False, False]
    print("\nAll checks passed.")
