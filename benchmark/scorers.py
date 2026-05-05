"""Scorers for the RTLLM generate-and-test benchmark.

Three scorer factories run in this order — each later one reads
`state.scores` written by the earlier ones:

  1. `rtllm_make_passes` — correctness, runs hidden golden testbench
  2. `golden_ppa`        — golden_delay (ns), golden_area (µm²),
                           golden_power (µW), golden_ppa_score =
                           1 / (delay·area·power) for the golden
                           reference. One yosys→nangate45 + OpenSTA pass
                           per sample. Runs unconditionally — golden
                           values are design metadata, independent of
                           the agent.
  3. `openroad_ppa`      — delay (ns), area (µm²), power (µW), composite
                           ppa_score = 1 / (delay·area·power), and
                           relative_ppa_score = ppa_score(agent) /
                           ppa_score(golden). Gated on the testbench
                           passing; reads the golden product from
                           `golden_ppa` instead of re-synthesizing.
                           Despite the "post-P&R" framing, this is STA
                           on the linked netlist — no real placement or
                           routing.
"""
import asyncio
import re
import tempfile
from pathlib import Path

import pyslang

from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    mean,
    scorer,
    stderr,
)
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox

from server.openroad_ppa import (
    build_openroad_tcl,
    build_yosys_script,
    parse_ppa_report,
)

# Name of the sibling sandbox service in compose.yaml that the scorer uses
# to run the hidden golden testbench. The agent never has access to it.
SCORER_SANDBOX = "scorer"

# Glob-based makefile used only at scoring time, after we've staged the
# agent's design files alongside the golden testbench in the sibling
# 'scorer' sandbox. Globbing keeps it agnostic to how the agent split the
# design across files. The golden testbench's top module name varies across
# RTLLM (`testbench`, `add16_tb`, `tb_RAM`, `main`, …) — we detect it per
# sample and pin it as the elaboration root to avoid Verilator picking an
# arbitrary top when multiple unconnected roots are present.
def _verilator_makefile(testbench_top: str) -> str:
    return f"""\
.PHONY: vcs sim clean

vcs:
\tverilator --binary --timing -Wno-fatal -j 0 --top-module {testbench_top} -o simv $(wildcard *.v *.sv)

sim:
\t./obj_dir/simv | tee run.log

clean:
\trm -rf obj_dir *.log run.log output.txt
"""

# Top-level `module testbench` declaration — used to tell the agent's own
# testbench file apart from the design files when copying out for scoring.
_TESTBENCH_MODULE_RE = re.compile(r"^\s*module\s+testbench\b", re.MULTILINE)

_SCORING_TIMEOUT_S = 300


def _files_in_hierarchy(
    files: dict[str, str], top: str
) -> tuple[set[str], str | None]:
    r"""Return the subset of file names whose contents define a module,
    package, or `\`include` source reachable from `top`'s elaborated hierarchy.

    Files are written to a temp dir and parsed with pyslang so that
    `\`include` resolution works against sibling files. The hierarchy walk
    starts at the InstanceSymbol named `top`, recurses into instantiated
    submodules and surviving generate blocks, and records the file each
    DefinitionSymbol / imported PackageSymbol / IncludeMetadata came from.

    Returns ({}, error) if `top` is not defined or cannot be elaborated.
    """
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for name, text in files.items():
            (td_path / name).write_text(text)

        sm = pyslang.SourceManager()
        sm.addUserDirectories(td)
        comp = pyslang.Compilation()

        # Map slang's per-file BufferID back to the agent's filename. Each
        # registered SyntaxTree gets its own primary buffer; \`include
        # processing creates additional buffers we map by basename below.
        buf_to_name: dict[int, str] = {}
        trees: list[tuple[str, "pyslang.SyntaxTree"]] = []
        for name in files:
            tree = pyslang.SyntaxTree.fromFile(str(td_path / name), sm)
            comp.addSyntaxTree(tree)
            trees.append((name, tree))
            buf_to_name[tree.root.sourceRange.start.buffer.id] = name

        if comp.tryGetDefinition(top, comp.getRoot()) is None:
            return set(), f"slang: top module {top!r} not found in agent files"

        root = comp.getRoot()
        matched = [i for i in root.topInstances if i.name == top]
        if not matched:
            return set(), f"slang: could not elaborate {top!r} as a top instance"

        kept_buffers: set[int] = set()

        def _record(loc) -> None:
            try:
                kept_buffers.add(loc.buffer.id)
            except Exception:
                pass

        def _visit(symbol) -> None:
            kind = symbol.kind
            if kind == pyslang.SymbolKind.Instance:
                defn = symbol.definition
                if defn is not None:
                    _record(defn.location)
                for child in symbol.body:
                    _visit(child)
            elif kind in (
                pyslang.SymbolKind.ExplicitImport,
                pyslang.SymbolKind.WildcardImport,
            ):
                pkg = getattr(symbol, "package", None)
                if pkg is not None:
                    _record(pkg.location)
            elif kind in (
                pyslang.SymbolKind.GenerateBlock,
                pyslang.SymbolKind.GenerateBlockArray,
            ):
                for child in symbol:
                    _visit(child)

        for inst in matched:
            _visit(inst)

        # `\`include` directives in any reachable file pull in their target
        # files too. Map by basename since slang's include buffer differs
        # from the buffer assigned when we registered the included file
        # standalone.
        for name, tree in trees:
            primary = tree.root.sourceRange.start.buffer.id
            if primary not in kept_buffers:
                continue
            for inc in tree.getIncludeDirectives():
                inc_basename = Path(inc.path).name
                for fname in files:
                    if Path(fname).name == inc_basename:
                        kept_buffers.add(inc.buffer.id)
                        buf_to_name[inc.buffer.id] = fname
                        break

        return {buf_to_name[b] for b in kept_buffers if b in buf_to_name}, None


async def _collect_dut_files(design: str) -> tuple[dict[str, str], str | None]:
    """Read the agent's Verilog sources, dropping any file that defines a
    `module testbench` (the agent's own testbench — replaced at scoring time
    by the hidden golden testbench).

    Returns (files, error). On success error is None; on failure files is
    empty and error explains what went wrong (with extra detail when the
    design file was dropped because the agent inlined the testbench).
    """
    sbox = sandbox("solver")
    ls = await sbox.exec(["sh", "-c", "ls *.v *.sv 2>/dev/null"])
    names = [n for n in ls.stdout.split() if n]
    if not names:
        return {}, "agent produced no Verilog files in sandbox cwd"

    files: dict[str, str] = {}
    dropped_as_testbench: list[str] = []
    for name in names:
        content = await sbox.read_file(name)
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        if _TESTBENCH_MODULE_RE.search(content):
            dropped_as_testbench.append(name)
            continue
        files[name] = content

    if f"{design}.v" not in files:
        if f"{design}.v" in dropped_as_testbench:
            return {}, (
                f"{design}.v was dropped because it contains a `module testbench` "
                "declaration — keep the design and the testbench in separate files"
            )
        return {}, (
            f"agent did not produce {design}.v "
            f"(found design files: {sorted(files)}, "
            f"dropped as testbenches: {sorted(dropped_as_testbench)})"
        )

    kept, err = _files_in_hierarchy(files, design)
    if err is not None:
        return {}, err
    files = {n: c for n, c in files.items() if n in kept}
    if f"{design}.v" not in files:
        return {}, f"internal: {design}.v dropped by hierarchy filter"
    return files, None


async def _run_golden_in_sibling(
    files: dict[str, str],
    golden_testbench_text: str,
    golden_testbench_top: str,
) -> tuple[int, str, str]:
    """Stage the agent's design files + golden testbench + makefile into the
    sibling 'scorer' sandbox container (defined in compose.yaml — Inspect
    spawns it per-sample and the agent never has access to it) and run the
    grading make recipe there.

    Returns (rc, stdout, stderr). Raises asyncio.TimeoutError on overrun.
    """
    sbox = sandbox(SCORER_SANDBOX)

    # Defensive cleanup — Inspect provisions a fresh container per sample,
    # but if a future change reuses the sibling we don't want stale files.
    await sbox.exec(["sh", "-c", "rm -rf /workspace/* /workspace/.[!.]*"])

    for name, content in files.items():
        await sbox.write_file(f"/workspace/{name}", content)
    await sbox.write_file("/workspace/testbench.v", golden_testbench_text)
    await sbox.write_file("/workspace/makefile", _verilator_makefile(golden_testbench_top))

    result = await asyncio.wait_for(
        sbox.exec(
            ["sh", "-c", "make clean >/dev/null 2>&1 || true; make vcs && make sim"],
        ),
        timeout=_SCORING_TIMEOUT_S,
    )
    return result.returncode, result.stdout, result.stderr


@scorer(metrics=[accuracy(), stderr()])
def rtllm_make_passes() -> Scorer:
    """Score by running the hidden golden testbench against the agent's design
    in a sibling sandbox the agent never had access to.

    Per-sample inputs (design name, golden testbench text) come from
    `state.metadata` — see `_build_sample` in tasks.py.
    """

    async def score(state: TaskState, target: Target) -> Score:
        design = state.metadata["design"]
        golden_text = state.metadata["golden_testbench_text"]
        golden_tb_top = state.metadata["golden_testbench_top"]
        files, err = await _collect_dut_files(design)
        if err:
            return Score(value=INCORRECT, explanation=err)

        try:
            rc, stdout, stderr_out = await _run_golden_in_sibling(
                files, golden_text, golden_tb_top
            )
        except asyncio.TimeoutError:
            return Score(
                value=INCORRECT,
                explanation=f"golden testbench run timed out after {_SCORING_TIMEOUT_S}s",
            )

        if rc != 0:
            return Score(
                value=INCORRECT,
                answer=stdout,
                explanation=f"golden testbench run failed (rc={rc}):\n{stderr_out[-2000:]}",
            )
        if "Passed" not in stdout:
            return Score(
                value=INCORRECT,
                answer=stdout,
                explanation="golden testbench did not print 'Passed'",
            )
        return Score(value=CORRECT, answer=stdout)

    return score


_PPA_KEYS = ("delay", "area", "power", "ppa_score", "relative_ppa_score")
_NAN_PPA = {k: float("nan") for k in _PPA_KEYS}

_GOLDEN_PPA_KEYS = ("golden_delay", "golden_area", "golden_power", "golden_ppa_score")
_NAN_GOLDEN_PPA = {k: float("nan") for k in _GOLDEN_PPA_KEYS}


async def _measure_ppa(
    sbox, sources: list[str], top: str, label: str
) -> tuple[dict[str, float] | None, str | None]:
    """Synthesize the given Verilog sources to nangate45 and run OpenSTA.
    Returns ({delay_ns, area_um2, power_uw}, None) on success or
    (None, error_msg) on any failure. `label` ("agent"/"golden") namespaces
    tmp files and error messages so the two passes don't trip over each
    other.

    Synthesis recipe, OpenSTA TCL, and report parsing are imported from
    `server.openroad_ppa` so the agent's `openroad_ppa` MCP tool measures
    PPA the same way this scorer does.
    """
    netlist_v = f"/tmp/{label}_netlist.v"
    tcl_path = f"/tmp/openroad_ppa_{label}.tcl"

    synth = await sbox.exec(
        ["yosys", "-q", "-p", build_yosys_script(sources, top, netlist_v)]
    )
    if not synth.success:
        return None, f"{label} yosys failed (rc={synth.returncode}):\n{synth.stderr[-2000:]}"

    await sbox.write_file(tcl_path, build_openroad_tcl(top, netlist_v))
    try:
        ord_run = await asyncio.wait_for(
            sbox.exec(["openroad", "-no_init", "-exit", tcl_path]),
            timeout=120,
        )
    except asyncio.TimeoutError:
        return None, f"{label} openroad timed out after 120s"

    if not ord_run.success:
        return None, f"{label} openroad failed (rc={ord_run.returncode}):\n{ord_run.stdout[-2000:]}"

    metrics, err = parse_ppa_report(ord_run.stdout)
    if err:
        return None, f"{label}: {err}"
    return metrics, None


@scorer(metrics={k: [mean(), stderr()] for k in _GOLDEN_PPA_KEYS})
def golden_ppa() -> Scorer:
    """Synthesize the golden reference design to nangate45, run OpenSTA, and
    report golden_delay (ns), golden_area (µm²), golden_power (µW), and
    golden_ppa_score = 1 / (delay·area·power).

    Runs unconditionally — the golden reference is a property of the dataset,
    independent of the agent. `openroad_ppa` reads the result from
    `state.scores["golden_ppa"]` to compute `relative_ppa_score` instead of
    re-synthesizing the reference.

    Per-sample inputs (design name, golden reference text, golden top module)
    come from `state.metadata` — see `_build_sample` in tasks.py.
    """

    async def score(state: TaskState, target: Target) -> Score:
        design = state.metadata["design"]
        golden_text = state.metadata["golden_reference_text"]
        golden_top = state.metadata["golden_top"]

        sbox = sandbox(SCORER_SANDBOX)
        golden_v_path = f"/tmp/golden_{design}.v"
        await sbox.write_file(golden_v_path, golden_text)

        golden_m, err = await _measure_ppa(sbox, [golden_v_path], golden_top, "golden")
        if err:
            return Score(value=_NAN_GOLDEN_PPA, explanation=err)

        delay_ns, area_um2, power_uw = golden_m["delay"], golden_m["area"], golden_m["power"]
        product = delay_ns * area_um2 * power_uw
        ppa = 1.0 / product if product > 0 else float("nan")

        return Score(
            value={
                "golden_delay": delay_ns,
                "golden_area": area_um2,
                "golden_power": power_uw,
                "golden_ppa_score": ppa,
            },
            answer=(
                f"golden_delay={delay_ns:.3f}ns golden_area={area_um2:.1f}um^2 "
                f"golden_power={power_uw:.3f}uW golden_ppa_score={ppa:.3e}"
            ),
            explanation=(
                f"golden: delay={delay_ns:.3f} ns, area={area_um2:.1f} µm², "
                f"power={power_uw:.3f} µW, ppa_score={ppa:.3e} "
                "(nangate45, 1 ns clock)"
            ),
        )

    return score


@scorer(metrics={k: [mean(), stderr()] for k in _PPA_KEYS})
def openroad_ppa() -> Scorer:
    """Synthesize the agent's design to nangate45, run OpenSTA, and report
    delay (ns), area (µm²), power (µW), `ppa_score = 1 / (delay·area·power)`
    (higher is better), and `relative_ppa_score = ppa_score(agent) /
    ppa_score(golden)` — i.e. `(delay·area·power)_golden /
    (delay·area·power)_agent`. >1 means the agent beat the reference on the
    composite metric.

    The clock is the design's `clk`/`clock`/`i_clk`/`clk_i` port at 1 ns when
    one exists, else a virtual 1 ns clock. Input/output delays are pinned to
    zero so STA finds combinational paths in flop-less designs. Absolute
    numbers aren't physically meaningful (no placement, no wire RC), but the
    relative score cancels the methodology and is comparable across designs.

    Gated on the testbench passing — returns all-NaN otherwise so per-key
    means are computed only over correct runs (Inspect filters NaN per-key
    for dict-valued scores). Reads the golden reference's PPA from
    `state.scores["golden_ppa"]`, so `golden_ppa` must run first.

    Per-sample inputs come from `state.metadata` — see `_build_sample` in
    tasks.py.
    """

    async def score(state: TaskState, target: Target) -> Score:
        pass_score = (state.scores or {}).get("rtllm_make_passes")
        if pass_score is None or pass_score.value != CORRECT:
            return Score(value=_NAN_PPA, explanation="testbench did not pass — PPA omitted")

        golden_score = (state.scores or {}).get("golden_ppa")
        if golden_score is None:
            return Score(value=_NAN_PPA, explanation="golden_ppa scorer did not run")
        golden_vals = golden_score.value
        if not isinstance(golden_vals, dict) or any(
            isinstance(v, float) and v != v for v in golden_vals.values()
        ):
            return Score(
                value=_NAN_PPA,
                explanation=f"golden ppa unavailable: {golden_score.explanation}",
            )

        design = state.metadata["design"]
        sbox = sandbox(SCORER_SANDBOX)

        # The agent's hierarchy files were already staged into `/workspace/`
        # by `_run_golden_in_sibling` during `rtllm_make_passes` (which gates
        # this scorer); re-discover the file set so multi-file designs reach
        # yosys whole.
        agent_files, err = await _collect_dut_files(design)
        if err:
            return Score(value=_NAN_PPA, explanation=err)
        agent_sources = [f"/workspace/{n}" for n in agent_files]

        agent_m, err = await _measure_ppa(sbox, agent_sources, design, "agent")
        if err:
            return Score(value=_NAN_PPA, explanation=err)

        delay_ns, area_um2, power_uw = agent_m["delay"], agent_m["area"], agent_m["power"]
        agent_product = delay_ns * area_um2 * power_uw
        golden_product = (
            golden_vals["golden_delay"]
            * golden_vals["golden_area"]
            * golden_vals["golden_power"]
        )
        ppa_score = 1.0 / agent_product if agent_product > 0 else float("nan")
        relative = (
            golden_product / agent_product
            if agent_product > 0 and golden_product > 0
            else float("nan")
        )

        return Score(
            value={
                "delay": delay_ns,
                "area": area_um2,
                "power": power_uw,
                "ppa_score": ppa_score,
                "relative_ppa_score": relative,
            },
            answer=(
                f"delay={delay_ns:.3f}ns area={area_um2:.1f}um^2 "
                f"power={power_uw:.3f}uW ppa_score={ppa_score:.3e} "
                f"relative_ppa_score={relative:.3f}"
            ),
            explanation=(
                f"agent: delay={delay_ns:.3f} ns, area={area_um2:.1f} µm², "
                f"power={power_uw:.3f} µW. golden: delay={golden_vals['golden_delay']:.3f} ns, "
                f"area={golden_vals['golden_area']:.1f} µm², "
                f"power={golden_vals['golden_power']:.3f} µW. "
                f"ppa_score={ppa_score:.3e}, relative_ppa_score={relative:.3f} "
                "(nangate45, 1 ns clock)"
            ),
        )

    return score
