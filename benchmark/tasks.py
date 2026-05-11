"""Generate Verilog designs across the RTLLM benchmark and score them.

Run with:
    inspect eval benchmark/tasks.py
    inspect eval benchmark/tasks.py --sample-id adder_8bit
"""
import fnmatch
import re
import sys
from pathlib import Path

# inspect_ai loads task files via SourceFileLoader without adding their
# parent directory to sys.path, so the `benchmark` package isn't importable
# by name yet. Add the repo root before importing siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspect_ai import Task, task
from inspect_ai.dataset import Sample

from benchmark.scorers import (
    golden_ppa,
    openroad_ppa,
    rtllm_make_passes,
)
from benchmark.solvers import rtllm_react_solver

REPO_ROOT = Path(__file__).parent.parent
RTLLM_ROOT = REPO_ROOT / "external" / "RTLLM"
SANDBOX_COMPOSE = REPO_ROOT / "sandbox" / "compose.yaml"

# Files in the upstream RTLLM design folder that we never deliver to the
# agent: ground-truth solutions, the upstream VCS makefile, and crucially
# the golden testbench — the agent must write its own and only sees the
# natural-language spec. The golden testbench is run separately at scoring
# time in a fresh container.
SKIP_COPY_PATTERNS = (
    "verified_*.v",
    "makefile",
    "Makefile",
    "testbench.v",
)


def _design_files(folder: Path, design: str) -> dict[str, str]:
    """Map sandbox-relative path → host path for files the agent should see.

    The agent only receives `design_description.txt`. The golden testbench,
    ground-truth solutions, and the upstream VCS makefile are all withheld;
    grading happens in a sibling sandbox at score time (see `rtllm_make_passes`).
    """
    skip_exact = {f"{design}.v"}
    out: dict[str, str] = {}
    for src in folder.iterdir():
        if not src.is_file() or src.name in skip_exact:
            continue
        if any(fnmatch.fnmatch(src.name, pat) for pat in SKIP_COPY_PATTERNS):
            continue
        out[src.name] = str(src.resolve())
    if "design_description.txt" not in out:
        sys.exit(f"Design folder {folder} missing required file: design_description.txt")
    return out


def _detect_golden_top(text: str, design: str) -> str | None:
    """Return the top module name in a `verified_<design>.v` reference.

    Naming in RTLLM golden references is inconsistent — some files use
    `module <design>` and others `module verified_<design>`. We try both
    in order and pick whichever the file actually declares.
    """
    for candidate in (design, f"verified_{design}"):
        if re.search(rf"\bmodule\s+{re.escape(candidate)}\b", text):
            return candidate
    return None


# Matches the first `module <name>` declaration in a Verilog source. RTLLM
# testbench files each declare exactly one module, so the first match is the
# top. The names vary widely (`testbench`, `add16_tb`, `tb_RAM`, `main`, …),
# so we can't pin a single name in the scorer's makefile.
_MODULE_DECL_RE = re.compile(r"^\s*module\s+(\w+)", re.MULTILINE)


def _detect_testbench_top(text: str) -> str | None:
    """Return the module name declared in a testbench file, or None if no
    `module` declaration is found."""
    m = _MODULE_DECL_RE.search(text)
    return m.group(1) if m else None


def _build_sample(folder: Path) -> Sample | None:
    """Build a Sample for one design folder, or return None if the folder is
    missing the golden artifacts we need (testbench / verified reference /
    detectable top module). Designs without these can't be scored, so they
    don't belong in the dataset.
    """
    design = folder.name
    testbench = folder / "testbench.v"
    if not testbench.is_file():
        return None
    reference = folder / f"verified_{design}.v"
    if not reference.is_file():
        return None
    reference_text = reference.read_text()
    golden_top = _detect_golden_top(reference_text, design)
    if golden_top is None:
        return None
    testbench_text = testbench.read_text()
    testbench_top = _detect_testbench_top(testbench_text)
    if testbench_top is None:
        return None

    description = (folder / "design_description.txt").read_text()

    return Sample(
        id=design,
        input=f"Design name: `{design}`. Write your module to `{design}.v`.\n\n{description}",
        target="testbench prints 'Passed'",
        files=_design_files(folder, design),
        metadata={
            "design": design,
            "golden_testbench_text": testbench_text,
            "golden_testbench_top": testbench_top,
            "golden_reference_text": reference_text,
            "golden_top": golden_top,
        },
    )


def _build_dataset() -> list[Sample]:
    samples = [s for s in (_build_sample(f.parent) for f in RTLLM_ROOT.rglob("design_description.txt")) if s is not None]
    if not samples:
        sys.exit(f"No usable RTLLM designs found under {RTLLM_ROOT}")
    samples.sort(key=lambda s: s.id)
    return samples


@task
def rtllm_generate_and_test(message_limit: int = 40) -> Task:
    return Task(
        dataset=_build_dataset(),
        solver=rtllm_react_solver(),
        scorer=[
            rtllm_make_passes(),
            golden_ppa(),
            openroad_ppa(),
        ],
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        message_limit=message_limit,
    )
