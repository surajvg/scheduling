import React, { useEffect, useState } from "react";
import axios from "axios";
import { useSelector } from "react-redux";
import {
  Alert, Autocomplete, Box, Button, Collapse, IconButton, LinearProgress, MenuItem, Select, Stack, Tab, Tabs,
  TextField, Tooltip, Typography,
} from "@mui/material";
import {
  EventNote as ScheduleIcon, Refresh as RefreshIcon, ZoomIn as ZoomInIcon, ZoomOut as ZoomOutIcon,
} from "@mui/icons-material";
import GanttChart from "./GanttChart";
import EditOperationDialog from "./EditOperationDialog";
import ScheduleConfigPage from "./ScheduleConfigPage";

// Shape of GET /schedule/data with empty lists - used before the first load and when the request fails
const EMPTY_DATA = {
  generated_at: "",
  calendar: { shift_start: 6, shift_end: 20, shifts: [], holidays: [], half_days: [], working_exceptions: [] },
  machines: [],
  operators: [],
  production_orders: [],
  operations: [],
  warnings: [],
};

// one colour per production order (in the order of data.production_orders)
const PO_COLORS = [
  "#1e88e5", "#f59e0b", "#10b981", "#e11d48", "#8b5cf6", "#14b8a6",
  "#f97316", "#84cc16", "#ec4899", "#0ea5e9", "#a16207", "#6366f1",
];

// first moment of the day / week (Monday) / month / year that contains `date`
function periodStart(view, date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  if (view === "weekly") d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  if (view === "monthly") d.setDate(1);
  if (view === "yearly") d.setMonth(0, 1);
  return d;
}

// move `date` by n days / weeks / months / years
function addPeriod(view, date, n) {
  const d = new Date(date);
  if (view === "daily") d.setDate(d.getDate() + n);
  if (view === "weekly") d.setDate(d.getDate() + 7 * n);
  if (view === "monthly") d.setMonth(d.getMonth() + n);
  if (view === "yearly") d.setFullYear(d.getFullYear() + n);
  return d;
}

// Date -> "YYYY-MM-DD" (local time) for <input type="date">
function toYmd(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function machineLabel(m) {
  return m.machine_name && m.machine_name !== m.machine_id ? `${m.machine_id} - ${m.machine_name}` : m.machine_id;
}

const SchedulingPage = () => {
  // server address from Redux, same as RechartsGantt.js
  const apiBase = useSelector((state) => state.MROTDataSavingReducer?.configDetails)
    ?.project?.[0]?.ServerIP?.[0]?.PythonServerIP;

  const [data, setData] = useState(EMPTY_DATA);        // raw response of GET /schedule/data
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState(0);                    // 0 = Machine, 1 = Planned, 2 = Process Config
  const [view, setView] = useState("weekly");           // daily | weekly | monthly | yearly
  const [anchor, setAnchor] = useState(new Date());     // a date inside the visible period
  const [fromDate, setFromDate] = useState("");         // custom range "YYYY-MM-DD" (wins over the period)
  const [toDate, setToDate] = useState("");
  const [zoom, setZoom] = useState(1);
  const [machineFilter, setMachineFilter] = useState([]);
  const [partFilter, setPartFilter] = useState([]);
  const [poFilter, setPoFilter] = useState([]);
  const [editOp, setEditOp] = useState(null);           // operation open in the Edit dialog
  const [showWarnings, setShowWarnings] = useState(false);

  const loadSchedule = () => {
    if (!apiBase) {
      setError("Server address (PythonServerIP) is not configured yet.");
      return;
    }
    setLoading(true);
    setError("");
    axios.get(apiBase + "/schedule/data")
      .then((res) => setData({ ...EMPTY_DATA, ...res.data }))
      .catch((err) => {
        setData(EMPTY_DATA);
        setError("Could not load the schedule: " + (err.response?.data?.detail || err.message));
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadSchedule();
  }, [apiBase]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- visible time range: custom dates if both are filled, else the period around `anchor` ----
  let rangeStart = periodStart(view, anchor);
  let rangeEnd = addPeriod(view, rangeStart, 1);
  const customRange = fromDate && toDate && toDate >= fromDate;
  if (customRange) {
    rangeStart = new Date(fromDate + "T00:00:00");
    rangeEnd = new Date(toDate + "T00:00:00");
    rangeEnd.setDate(rangeEnd.getDate() + 1);           // include the "to" day
  }

  // < > arrows: move the custom range by its own length, otherwise one period
  const move = (direction) => {
    if (customRange) {
      const days = Math.round((rangeEnd - rangeStart) / 86400000) * direction;
      const f = new Date(rangeStart);
      const t = new Date(rangeEnd);
      f.setDate(f.getDate() + days);
      t.setDate(t.getDate() + days - 1);
      setFromDate(toYmd(f));
      setToDate(toYmd(t));
    } else {
      setAnchor(addPeriod(view, anchor, direction));
    }
  };

  // colour per PO = data.production_orders
  const poColor = {};
  data.production_orders.forEach((po, i) => {
    poColor[po.production_order] = PO_COLORS[i % PO_COLORS.length];
  });

  // filter options = data.machines / data.operations / data.production_orders
  const partOptions = [...new Set(data.operations.map((op) => op.part_number))].sort();
  const poOptions = data.production_orders.map((po) => po.production_order);

  // Production Order filter: also jump to the first operation of the chosen orders
  const pickOrders = (list) => {
    setPoFilter(list);
    const starts = data.operations
      .filter((op) => list.includes(op.production_order))
      .map((op) => new Date(tab === 1 ? op.planned_start : op.start_time));
    if (starts.length) {
      setFromDate("");
      setToDate("");
      setAnchor(new Date(Math.min(...starts)));
    }
  };

  // rows = data.machines (filtered)
  const rows = data.machines.filter((m) => machineFilter.length === 0 || machineFilter.includes(m.machine_id));

  // bars = data.operations; Machine Schedule uses machine_id/start_time/end_time/segments,
  //        Planned Schedule uses planned_machine_id/planned_start/planned_end/planned_segments
  const planned = tab === 1;
  const bars = data.operations
    .filter((op) => partFilter.length === 0 || partFilter.includes(op.part_number))
    .filter((op) => poFilter.length === 0 || poFilter.includes(op.production_order))
    .map((op) => ({
      op,
      machine_id: planned ? op.planned_machine_id : op.machine_id,
      start: new Date(planned ? op.planned_start : op.start_time),
      end: new Date(planned ? op.planned_end : op.end_time),
      // working pieces (op.segments / op.planned_segments); one piece when the server sends none
      segments: ((planned ? op.planned_segments : op.segments) || [[planned ? op.planned_start : op.start_time, planned ? op.planned_end : op.end_time]])
        .map(([s, e]) => ({ start: new Date(s), end: new Date(e) })),
    }));

  return (
    <Box sx={{ p: 3, bgcolor: "#f7f8fb", minHeight: "100%", fontFamily: (theme) => theme.typography.fontFamily }}>
      <Box sx={{ bgcolor: "#fff", borderRadius: "10px", px: 2.5, py: 2, mb: 2.5, display: "flex", alignItems: "center", gap: 1.25, border: "1px solid #e3e7ee" }}>
        <ScheduleIcon />
        <Typography sx={{ fontSize: 19, fontWeight: 500 }}>Production Schedule</Typography>
        {/* data.generated_at */}
        {data.generated_at && (
          <Typography sx={{ fontSize: 12, color: "#6b7280", ml: "auto" }}>
            Calculated {new Date(data.generated_at).toLocaleString("en-IN")}
          </Typography>
        )}
      </Box>

      <Box sx={{ bgcolor: "#fff", borderRadius: "10px", border: "1px solid #e3e7ee", px: 2.5, pt: 1, pb: 2.5 }}>
        <Tabs value={tab} onChange={(e, v) => setTab(v)} sx={{ borderBottom: "1px solid #e3e7ee", mb: 2 }}>
          <Tab label="Machine Schedule" sx={{ textTransform: "none" }} />
          <Tab label="Planned Schedule" sx={{ textTransform: "none" }} />
          <Tab label="Process Config" sx={{ textTransform: "none" }} />
        </Tabs>

        {tab === 2 ? (
          <ScheduleConfigPage apiBase={apiBase} onChanged={loadSchedule} />
        ) : (
          <>
            {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

            {/* data.warnings */}
            {data.warnings.length > 0 && (
              <Alert severity="warning" sx={{ mb: 2 }}
                action={<Button color="inherit" size="small" onClick={() => setShowWarnings(!showWarnings)}>{showWarnings ? "Hide" : "Show"}</Button>}>
                {data.warnings.length} scheduling warning(s)
                <Collapse in={showWarnings}>
                  <Box component="ul" sx={{ m: 0, mt: 1, pl: 2.5, maxHeight: 200, overflow: "auto" }}>
                    {data.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </Box>
                </Collapse>
              </Alert>
            )}

            {/* ---- toolbar ---- */}
            <Stack direction="row" flexWrap="wrap" alignItems="center" sx={{ gap: 1.25, mb: 2 }}>
              <Select size="small" value={view} sx={{ width: 115 }}
                onChange={(e) => { setView(e.target.value); setFromDate(""); setToDate(""); setZoom(1); }}>
                <MenuItem value="daily">Daily</MenuItem>
                <MenuItem value="weekly">Weekly</MenuItem>
                <MenuItem value="monthly">Monthly</MenuItem>
                <MenuItem value="yearly">Yearly</MenuItem>
              </Select>

              <TextField size="small" type="date" label="Start Date" value={fromDate} sx={{ width: 155 }}
                InputLabelProps={{ shrink: true }} onChange={(e) => setFromDate(e.target.value)} />
              <TextField size="small" type="date" label="End Date" value={toDate} sx={{ width: 155 }}
                InputLabelProps={{ shrink: true }} onChange={(e) => setToDate(e.target.value)} />

              {/* options = data.machines */}
              <Autocomplete multiple size="small" limitTags={1} disableCloseOnSelect sx={{ width: 220 }}
                options={data.machines.map((m) => m.machine_id)} value={machineFilter}
                getOptionLabel={(id) => { const m = data.machines.find((x) => x.machine_id === id); return m ? machineLabel(m) : id; }}
                onChange={(e, v) => setMachineFilter(v)}
                renderInput={(params) => <TextField {...params} placeholder={machineFilter.length ? "" : "Select Machines"} />} />

              {/* options = data.operations[].part_number */}
              <Autocomplete multiple size="small" limitTags={1} disableCloseOnSelect sx={{ width: 210 }}
                options={partOptions} value={partFilter} onChange={(e, v) => setPartFilter(v)}
                renderInput={(params) => <TextField {...params} placeholder={partFilter.length ? "" : "Select Part Number"} />} />

              {/* options = data.production_orders */}
              <Autocomplete multiple size="small" limitTags={1} disableCloseOnSelect sx={{ width: 230 }}
                options={poOptions} value={poFilter} onChange={(e, v) => pickOrders(v)}
                renderInput={(params) => <TextField {...params} placeholder={poFilter.length ? "" : "Select Production Orders"} />} />

              <Tooltip title="Zoom in">
                <IconButton onClick={() => setZoom(Math.min(zoom + 0.5, 6))}><ZoomInIcon /></IconButton>
              </Tooltip>
              <Tooltip title="Zoom out">
                <IconButton onClick={() => setZoom(Math.max(zoom - 0.5, 1))}><ZoomOutIcon /></IconButton>
              </Tooltip>

              <Button variant="contained" startIcon={<RefreshIcon />} onClick={loadSchedule} disabled={loading}
                sx={{ textTransform: "none", bgcolor: "#5b6573", "&:hover": { bgcolor: "#4a5361" } }}>
                Refresh
              </Button>
            </Stack>

            {loading && <LinearProgress sx={{ mb: 1 }} />}

            {/* rows = data.machines, bars = data.operations, shading = data.calendar */}
            <GanttChart
              machines={rows}
              bars={bars}
              rangeStart={rangeStart}
              rangeEnd={rangeEnd}
              zoom={zoom}
              holidays={data.calendar.holidays || []}
              halfDays={data.calendar.half_days || []}
              poColor={poColor}
              onBarClick={tab === 0 ? (op) => setEditOp(op) : null}
              onPrev={() => move(-1)}
              onNext={() => move(1)}
            />

            {/* legend = data.production_orders */}
            <Box sx={{ mt: 2.5, pt: 1.5, borderTop: "1px solid #e3e7ee" }}>
              <Typography sx={{ fontSize: 13, fontWeight: 600, mb: 1 }}>Production Orders</Typography>
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: "6px 18px" }}>
                {data.production_orders.map((po) => (
                  <Box key={po.production_order} sx={{ display: "flex", alignItems: "center", gap: 0.75, fontSize: 12, color: "#4b5563" }}>
                    <Box sx={{ width: 13, height: 13, borderRadius: "3px", bgcolor: poColor[po.production_order] }} />
                    {po.production_order} ({po.part_number}, {po.quantity} pcs, {po.status})
                  </Box>
                ))}
              </Box>
              <Typography sx={{ fontSize: 12, color: "#6b7280", mt: 1 }}>
                Faded bar = completed, outlined bar = in progress, shaded column = non-working day
                {tab === 0 ? ". Click a bar to move it to another machine or time." : "."}
              </Typography>
            </Box>
          </>
        )}
      </Box>

      <EditOperationDialog
        open={Boolean(editOp)}
        op={editOp}
        machines={data.machines}
        apiBase={apiBase}
        onClose={() => setEditOp(null)}
        onSaved={() => { setEditOp(null); loadSchedule(); }}
      />
    </Box>
  );
};

export default SchedulingPage;
