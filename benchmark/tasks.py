"""Generate a Verilog design and score it against the RTLLM testbench.

Run with:
    inspect eval benchmark/tasks.py -T design=adder_8bit
"""
import fnmatch
import sys
from pathlib import Path

# inspect_ai loads task files via SourceFileLoader without adding their
# parent directory to sys.path, so the `benchmark` package isn't importable
# by name yet. Add the repo root before importing siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inspect_ai import Task, task
from inspect_ai.dataset import Sample

from benchmark.scorers import (
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


def find_design(name: str) -> Path:
    for path in RTLLM_ROOT.rglob(name):
        if path.is_dir() and (path / "design_description.txt").exists():
            return path
    sys.exit(f"Design not found: {name}")


def design_files(folder: Path, design: str) -> dict[str, str]:
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
        sys.exit("Design folder missing required file: design_description.txt")
    return out


@task
def rtllm_generate_and_test(design: str, message_limit: int = 40) -> Task:
    folder = find_design(design)
    description = (folder / "design_description.txt").read_text()
    golden_testbench = folder / "testbench.v"
    if not golden_testbench.is_file():
        sys.exit(f"Golden testbench not found: {golden_testbench}")
    golden_reference = folder / f"verified_{design}.v"
    if not golden_reference.is_file():
        sys.exit(f"Golden reference not found: {golden_reference}")

    sample = Sample(
        id=design,
        input=description,
        target="testbench prints 'Passed'",
        files=design_files(folder, design),
    )

    return Task(
        dataset=[sample],
        solver=rtllm_react_solver(design),
        scorer=[
            rtllm_make_passes(design, golden_testbench),
            openroad_ppa(design, golden_reference),
        ],
        sandbox=("docker", str(SANDBOX_COMPOSE)),
        message_limit=message_limit,
    )
