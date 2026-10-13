import React, { useEffect, useRef, useState } from "react";
import { Box, IconButton, Tooltip, Typography } from "@mui/material";
import { ChevronLeft as ChevronLeftIcon, ChevronRight as ChevronRightIcon } from "@mui/icons-material";

const LABEL_WIDTH = 240;   // machine names column
const HEADER_HEIGHT = 30;
const LANE_HEIGHT = 34;    // one bar line inside a machine row
const DAY = 86400000;

function fmt(d) {
  return d ? d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "-";
}

function machineLabel(m) {
  return m.machine_name && m.machine_name !== m.machine_id ? `${m.machine_id} - ${m.machine_name}` : m.machine_id;
}

/**
 * Plain-div Gantt chart.
 *  machines : data.machines (one row each)
 *  bars     : [{ op, machine_id, start: Date, end: Date, segments: [{ start: Date, end: Date }] }]
 *             (op = one entry of data.operations, segments = its working pieces)
 *  holidays : data.calendar.holidays,  halfDays : data.calendar.half_days
 */
const GanttChart = ({ machines, bars, rangeStart, rangeEnd, zoom, holidays, halfDays, poColor, onBarClick, onPrev, onNext }) => {
  const boxRef = useRef(null);
  const [boxWidth, setBoxWidth] = useState(1000);
  const [now, setNow] = useState(new Date());

  // measure the available width once and on window resize; move the red "now" line every minute
  useEffect(() => {
    const measure = () => { if (boxRef.current) setBoxWidth(boxRef.current.clientWidth); };
    measure();
    window.addEventListener("resize", measure);
    const timer = setInterval(() => setNow(new Date()), 60000);
    return () => { window.removeEventListener("resize", measure); clearInterval(timer); };
  }, []);

  // x = (time - rangeStart) / (rangeEnd - rangeStart) * width
  const width = Math.max(300, (boxWidth - LABEL_WIDTH - 20) * zoom);
  const startMs = rangeStart.getTime();
  const endMs = rangeEnd.getTime();
  const xOf = (time) => ((new Date(time).getTime() - startMs) / (endMs - startMs)) * width;

  // ---- header: hours (range up to 2 days), days (up to 62 days) or months ----
  const span = endMs - startMs;
  const unit = span <= 2 * DAY ? "hour" : span <= 62 * DAY ? "day" : "month";
  const ticks = [];
  let t = new Date(rangeStart);
  while (t < rangeEnd) {
    const next = new Date(t);
    if (unit === "hour") next.setHours(next.getHours() + 1);
    if (unit === "day") next.setDate(next.getDate() + 1);
    if (unit === "month") next.setMonth(next.getMonth() + 1, 1);
    const x = xOf(t);
    const w = xOf(next < rangeEnd ? next : rangeEnd) - x;
    let label = "";
    if (unit === "hour") label = w > 22 ? String(t.getHours()).padStart(2, "0") : "";
    if (unit === "day") label = w > 70 ? t.toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short" }) : w > 18 ? String(t.getDate()) : "";
    if (unit === "month") label = w > 55 ? t.toLocaleDateString("en-IN", { month: "short", year: "numeric" }) : w > 25 ? t.toLocaleDateString("en-IN", { month: "short" }) : "";
    ticks.push({ x, w, label, key: t.getTime() });
    t = next;
  }

  // ---- shaded days: data.calendar.holidays (full) and data.calendar.half_days (light) ----
  const dayShades = [];
  holidays.forEach((h) => {
    const day = new Date(h.holiday_date + "T00:00:00");
    if (day.getTime() + DAY > startMs && day.getTime() < endMs) {
      dayShades.push({ key: "h" + h.holiday_date, x: xOf(day), w: xOf(day.getTime() + DAY) - xOf(day), title: h.name, color: "rgba(100,116,139,0.13)" });
    }
  });
  halfDays.forEach((d) => {
    const day = new Date(d + "T00:00:00");
    if (day.getTime() + DAY > startMs && day.getTime() < endMs) {
      dayShades.push({ key: "s" + d, x: xOf(day), w: xOf(day.getTime() + DAY) - xOf(day), title: "Half day", color: "rgba(245,158,11,0.08)" });
    }
  });

  // ---- rows: one per machine; bars that overlap on one machine are stacked in extra lanes ----
  const rows = machines.map((m) => {
    const mine = bars
      .filter((b) => b.machine_id === m.machine_id && b.end >= rangeStart && b.start < rangeEnd)
      .sort((a, b) => a.start - b.start);
    const laneEnds = [];                 // end time of the last bar in each lane
    const placed = mine.map((b) => {
      let lane = laneEnds.findIndex((end) => end <= b.start);
      if (lane === -1) {
        lane = laneEnds.length;
        laneEnds.push(b.end);
      } else {
        laneEnds[lane] = b.end;
      }
      return { ...b, lane };
    });
    return { machine: m, bars: placed, height: Math.max(1, laneEnds.length) * LANE_HEIGHT + 6 };
  });

  const showNow = now >= rangeStart && now < rangeEnd;

  // tooltip content = fields of one data.operations entry
  const tooltip = (bar) => {
    const op = bar.op;
    const line = (k, v) => (
      <Box sx={{ display: "flex", gap: 0.5 }}>
        <span style={{ opacity: 0.75 }}>{k}:</span><b>{v === null || v === undefined || v === "" ? "-" : v}</b>
      </Box>
    );
    return (
      <Box sx={{ fontSize: 12, lineHeight: 1.5 }}>
        {line("Part Number", op.part_number)}
        {line("Machine", bar.machine_id)}
        {line("Production Order", op.production_order)}
        {line("Operation", `${op.operation_no} - ${op.step_name}`)}
        {line("Quantity", `${op.completed_qty}/${op.quantity} pcs done (${op.status})`)}
        {line("Mode", op.process_mode === "BATCH" ? `Batch (${op.batch_count} x ${op.batch_size})` : "Series")}
        {line("Operator", op.operator_name)}
        {line("Start", fmt(bar.start))}
        {line("End", fmt(bar.end))}
        {line("PDC", fmt(new Date(op.planned_end)))}
        {op.cooldown_until && line("Cooldown until", fmt(new Date(op.cooldown_until)))}
        {line("Waiting reason", op.waiting_reason)}
      </Box>
    );
  };

  return (
    <Box sx={{ fontFamily: (theme) => theme.typography.fontFamily }}>
      {/* prev / next period */}
      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", mb: 0.5 }}>
        <IconButton size="small" onClick={onPrev}><ChevronLeftIcon /></IconButton>
        <Typography sx={{ fontSize: 13, fontWeight: 600 }}>
          {rangeStart.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" })}
          {" – "}
          {new Date(endMs - 1).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" })}
        </Typography>
        <IconButton size="small" onClick={onNext}><ChevronRightIcon /></IconButton>
      </Box>

      <Box ref={boxRef} sx={{ overflow: "auto", maxHeight: 620, border: "1px solid #e3e7ee", borderRadius: "8px" }}>
        <Box sx={{ width: LABEL_WIDTH + width, position: "relative" }}>

          {/* header row */}
          <Box sx={{ display: "flex", position: "sticky", top: 0, zIndex: 4, bgcolor: "#fff", height: HEADER_HEIGHT, borderBottom: "1px solid #c9d0da" }}>
            <Box sx={{ width: LABEL_WIDTH, boxSizing: "border-box", flex: "none", position: "sticky", left: 0, zIndex: 5, bgcolor: "#fff", borderRight: "1px solid #c9d0da", px: 1, display: "flex", alignItems: "center", fontSize: 12, fontWeight: 600 }}>
              Machine
            </Box>
            <Box sx={{ position: "relative", width, flex: "none" }}>
              {ticks.map((tk) => (
                <Box key={tk.key} sx={{ position: "absolute", left: tk.x, width: tk.w, top: 0, bottom: 0, borderLeft: "1px solid #eceff4", fontSize: 11, color: "#4b5563", display: "flex", alignItems: "center", pl: 0.5, overflow: "hidden", whiteSpace: "nowrap" }}>
                  {tk.label}
                </Box>
              ))}
            </Box>
          </Box>

          {/* body */}
          <Box sx={{ position: "relative" }}>
            {/* background: grid lines, shaded days and the red "now" line */}
            <Box sx={{ position: "absolute", left: LABEL_WIDTH, top: 0, bottom: 0, width, pointerEvents: "none" }}>
              {dayShades.map((s) => (
                <Box key={s.key} title={s.title} sx={{ position: "absolute", left: s.x, width: s.w, top: 0, bottom: 0, bgcolor: s.color }} />
              ))}
              {ticks.map((tk) => (
                <Box key={tk.key} sx={{ position: "absolute", left: tk.x, top: 0, bottom: 0, width: "1px", bgcolor: "#eceff4" }} />
              ))}
              {showNow && (
                <Box sx={{ position: "absolute", left: xOf(now), top: 0, bottom: 0, width: "2px", bgcolor: "#ef4444", zIndex: 2 }} />
              )}
            </Box>

            {rows.length === 0 && (
              <Typography sx={{ p: 3, color: "#6b7280", fontSize: 13 }}>No machines to display.</Typography>
            )}

            {rows.map((row) => (
              <Box key={row.machine.machine_id} sx={{ display: "flex", height: row.height, borderBottom: "1px solid #eceff4" }}>
                <Box title={machineLabel(row.machine)} sx={{
                  width: LABEL_WIDTH, boxSizing: "border-box", flex: "none", position: "sticky", left: 0, zIndex: 3, bgcolor: "#fff",
                  borderRight: "1px solid #c9d0da", px: 1, display: "flex", flexDirection: "column", justifyContent: "center",
                  fontSize: 12, overflow: "hidden", whiteSpace: "nowrap",
                  fontStyle: row.machine.resource_type === "MANUAL" ? "italic" : "normal",
                }}>
                  <Box sx={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis" }}>{machineLabel(row.machine)}</Box>
                  <Box sx={{ color: "#6b7280", fontSize: 11, overflow: "hidden", textOverflow: "ellipsis" }}>{row.machine.work_centre}</Box>
                </Box>

                <Box sx={{ position: "relative", width, flex: "none" }}>
                  {row.bars.map((bar) => {
                    const color = poColor[bar.op.production_order] || "#1f6fe5";
                    const top = 3 + bar.lane * LANE_HEIGHT;
                    // working pieces (bar.segments) - nothing is drawn over nights / Sundays / holidays
                    const pieces = bar.segments
                      .filter((sg) => sg.end > rangeStart && sg.start < rangeEnd)
                      .map((sg) => {
                        const x = Math.max(0, xOf(sg.start));
                        return { x, w: Math.max(3, Math.min(width, xOf(sg.end)) - x) };
                      });
                    const widest = Math.max(0, ...pieces.map((p) => p.w));
                    const lineX = Math.max(0, xOf(bar.start));
                    const lineW = Math.min(width, xOf(bar.end)) - lineX;
                    return (
                      <React.Fragment key={bar.op.op_id}>
                        {/* thin dashed line joins the pieces of one operation across days off */}
                        {pieces.length > 1 && (
                          <Box sx={{ position: "absolute", left: lineX, width: lineW, top: top + (LANE_HEIGHT - 4) / 2,
                            borderTop: `2px dashed ${color}`, opacity: 0.6, pointerEvents: "none" }} />
                        )}
                        {pieces.map((p, i) => (
                          <Tooltip key={i} title={tooltip(bar)} placement="top" arrow followCursor>
                            <Box
                              onClick={() => onBarClick && onBarClick(bar.op)}
                              sx={{
                                position: "absolute", left: p.x, width: p.w, boxSizing: "border-box", top, height: LANE_HEIGHT - 4,
                                bgcolor: color, color: "#fff", borderRadius: "3px", px: p.w > 14 ? 0.5 : 0, overflow: "hidden",
                                fontSize: 11, lineHeight: 1.25, display: "flex", flexDirection: "column", justifyContent: "center",
                                whiteSpace: "nowrap", zIndex: 1, cursor: onBarClick ? "pointer" : "default",
                                opacity: bar.op.status === "COMPLETED" ? 0.5 : 1,
                                boxShadow: bar.op.status === "IN_PROGRESS" ? `inset 0 0 0 2px #fff, 0 0 0 1px ${color}` : "none",
                                "&:hover": { filter: "brightness(1.1)" },
                              }}
                            >
                              {/* text only in the widest piece */}
                              {p.w === widest && p.w > 30 && <Box sx={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis" }}>{bar.op.production_order}</Box>}
                              {p.w === widest && p.w > 60 && <Box sx={{ overflow: "hidden", textOverflow: "ellipsis" }}>{bar.op.operation_no} - {bar.op.step_name}</Box>}
                            </Box>
                          </Tooltip>
                        ))}
                      </React.Fragment>
                    );
                  })}
                </Box>
              </Box>
            ))}
          </Box>
        </Box>
      </Box>
    </Box>
  );
};

export default GanttChart;
