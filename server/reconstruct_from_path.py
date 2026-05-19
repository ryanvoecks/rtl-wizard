#!/usr/bin/env python3
"""Reconstruct the critical path as RTL from a Verilog/SystemVerilog file.

Runs yosys internally (read_verilog -> hierarchy -auto-top -> proc -> flatten
-> opt -> techmap -> opt -> ltp -noff), parses the longest-path dump, and
emits one line per step prefixed with `<basename>:<lineno>:` showing the
original RTL line where the wire was assigned.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

from util import eda_env

STEP_RE = re.compile(r"^\s*(\d+):\s+(\S+)(?:\s+\[(\d+)\])?")
FF_RE = re.compile(r"^\s*ff:\s+\\([\w.]+)(?:\s+\[(\d+)\])?")
SRC_RE = re.compile(r"\$(\w+)\$(/[^:]+):(\d+)\$")
OP_SYM = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "div": "/",
    "mod": "%",
    "neg": "-",
    "pos": "+",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
    "eq": "==",
    "ne": "!=",
    "eqx": "===",
    "nex": "!==",
    "and": "&",
    "or": "|",
    "xor": "^",
    "xnor": "~^",
    "not": "~",
    "logic_and": "&&",
    "logic_or": "||",
    "logic_not": "!",
    "shl": "<<",
    "shr": ">>",
    "sshl": "<<<",
    "sshr": ">>>",
    "reduce_and": "&",
    "reduce_or": "|",
    "reduce_xor": "^",
    "reduce_xnor": "~^",
    "reduce_bool": "!=0",
    "mux": "?:",
    "pmux": "case",
}
WIRE_RE = re.compile(r"^\\([\w.]+)$")
LHS_BLOCK_RE = re.compile(r"(?:wire|reg|assign)\s+(?:\[[^\]]+\]\s+)?(\w+)\s*=(?!=)")
LHS_NONBLOCK_RE = re.compile(r"(\{[^}]*\}|\b\w+)\s*(?:\[[^\]]*\])?\s*<=")
NOOP_RE = re.compile(r"^\s*(\w+)\s*(?:<=|=)\s*\1\s*;?\s*$")


def parse(text):
    """Tokenize the ltp dump into raw events. Each event is one path step
    classified as 'wire' (user-named net), 'line' (techmap cell carrying a
    source line), or 'synth' (auto-generated, e.g. $0\\c4[0:0])."""
    start, end, src = None, None, None
    events = []
    for raw in text.splitlines():
        if m := FF_RE.match(raw):
            end_bit = int(m.group(2)) if m.group(2) else 0
            end = (m.group(1), end_bit)
            continue
        m = STEP_RE.match(raw)
        if not m:
            continue
        step, tok, bit = int(m.group(1)), m.group(2), int(m.group(3) or 0)
        if a := SRC_RE.search(tok):
            src = src or a.group(2)
            events.append((step, "line", (int(a.group(3)), a.group(1))))
        elif w := WIRE_RE.match(tok):
            name = w.group(1)
            if start is None:
                start = (name, bit, step)
            else:
                events.append((step, "wire", name))
        else:
            events.append((step, "synth", None))
    # Combinational paths have no `ff:` terminator -- destination is the last wire.
    if end is None:
        for i in range(len(events) - 1, -1, -1):
            if events[i][1] == "wire":
                end = (events[i][2], 0)
                break
    return start, events, end, src


def index_source(path):
    """Return (lhs_name -> (lineno, text), full source as list)."""
    if not path or not Path(path).exists():
        return {}, []
    lines = Path(path).read_text().splitlines()
    name_idx = {}
    multi = set()
    for lineno, line in enumerate(lines, 1):
        if NOOP_RE.match(line):
            continue
        for name in extract_lhs_names(line):
            if name in name_idx:
                multi.add(name)
            name_idx[name] = (lineno, line.strip())
    # Drop names assigned in multiple places (e.g. registers written in many
    # case branches) -- no single line is the "right" one to display.
    for name in multi:
        del name_idx[name]
    return name_idx, lines


def leaf(name):
    return name.rsplit(".", 1)[-1]


def extract_lhs_names(line):
    """Identifiers assigned on this line. Handles `wire/reg/assign NAME =`,
    `NAME <=`, and concat targets `{NAME, NAME[bits], ...} <=`."""
    if m := LHS_BLOCK_RE.search(line):
        return [m.group(1)]
    if m := LHS_NONBLOCK_RE.search(line):
        lhs = m.group(1)
        if lhs.startswith("{"):
            return [
                pm.group(1)
                for part in lhs[1:-1].split(",")
                if (pm := re.match(r"\s*(\w+)", part))
            ]
        return [lhs]
    return []


def emit(start, events, end, src):
    name_idx, src_lines = index_source(src)
    base = Path(src).name if src else "?"

    def src_text(ln):
        return src_lines[ln - 1].strip() if 0 < ln <= len(src_lines) else None

    def wire_line(name):
        info = name_idx.get(leaf(name))
        return info[0] if info else None

    # Group consecutive events that share a source line into one chain entry.
    # A 'wire' event uses its declaration's line; 'synth' inherits from the
    # current group; 'line' uses its own. This collapses techmap fan-out into
    # one entry per RTL line, whether or not a user wire labels the output.
    chain = []  # (display, source_line, last_step, cell_type, shared)
    cur_line, cur_disp, cur_last, cur_cell, cur_shared = None, None, None, None, False
    last_kind = None
    for step, kind, detail in events:
        if kind == "line":
            line_num, cell = detail
            ev_line, ev_disp, ev_cell = line_num, None, cell
        elif kind == "wire":
            ev_line = wire_line(detail)
            if ev_line is None:
                ev_line, ev_disp, ev_cell = cur_line, cur_disp, cur_cell
            else:
                ev_disp, ev_cell = leaf(detail), None
        else:
            ev_line, ev_disp, ev_cell = cur_line, cur_disp, cur_cell
        if cur_line is not None and ev_line == cur_line:
            cur_last = step
            if ev_disp is not None:
                cur_disp = ev_disp
            if cur_cell is None and ev_cell is not None:
                cur_cell = ev_cell
        else:
            if cur_line is not None or cur_disp is not None:
                chain.append((cur_disp, cur_line, cur_last, cur_cell, cur_shared))
            cur_line, cur_disp, cur_last, cur_cell = ev_line, ev_disp, step, ev_cell
            # Direct line->line transition (no synth/wire between) means yosys's
            # opt_merge bridged two RTL lines via shared gates -- not real flow.
            cur_shared = last_kind == "line" and kind == "line"
        last_kind = kind
    if cur_line is not None or cur_disp is not None:
        chain.append((cur_disp, cur_line, cur_last, cur_cell, cur_shared))

    out = []
    prev_step = start[2] if start else 0
    total = 0
    for disp, ln, last_step, cell, shared in chain:
        delta = last_step - prev_step
        total += delta
        op = OP_SYM.get(cell, cell) if cell else None
        annot = f"[{op}] " if op else ""
        tail = "  // shared via opt_merge" if shared else ""
        txt = src_text(ln) if ln is not None else None
        if txt is not None:
            out.append(f"{base}:{ln}: +{delta:<2} {annot}{txt}{tail}")
        else:
            out.append(
                f"?:?: +{delta:<2} {annot}<synthesized: {disp or 'unknown'}>{tail}"
            )
        prev_step = last_step

    if end and leaf(end[0]) in name_idx:
        ln, txt = name_idx[leaf(end[0])]
        if not chain or chain[-1][1] != ln:
            out.append(f"{base}:{ln}: --  {txt}")

    start_name = start[0] if start else "?"
    end_name = end[0] if end else "?"
    header = f"// critical path: {start_name} -> {end_name} (depth {total})"
    return "\n".join([header] + out)


YOSYS_SCRIPT = "read_verilog {sv}{path}; hierarchy -auto-top; proc; flatten; opt; techmap; opt; ltp -noff"


def run_yosys(verilog_path):
    if not shutil.which("yosys"):
        raise RuntimeError("yosys not found on PATH")
    # Resolve to absolute so yosys embeds an absolute source path in cell
    # names; SRC_RE requires the leading `/` to match, and index_source
    # later reads the file back via that embedded path.
    abs_path = str(Path(verilog_path).expanduser().resolve())
    sv = "-sv " if abs_path.endswith(".sv") else ""
    script = YOSYS_SCRIPT.format(sv=sv, path=abs_path)
    res = subprocess.run(
        ["yosys", "-p", script],
        env=eda_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"yosys failed:\n{res.stderr or res.stdout}")
    return res.stdout


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <file.v | file.sv>")
    try:
        print(emit(*parse(run_yosys(sys.argv[1]))))
    except RuntimeError as e:
        sys.exit(str(e))
