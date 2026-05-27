#!/usr/bin/env python3
"""Produce a syntactically-equivalent rewrite of a DesignConfig by
reordering operands around commutative binary operators
(`+`, `*`, `&`, `|`, `^`, `==`, `!=`). Each operator the parser finds
is flipped with probability 0.5. Used to characterise the synthesis
tool noise floor by feeding the ORFS flow many "skins" of the same
logical design.

Usage:
    uv run eda_eval/rewriter.py --design aes_reference [--output-dir PATH] [--seed N]
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import random
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pyslang

from common.config import EQY_BIN, DesignConfig
from common.designs import resolve_design

FLIP_PROBABILITY = 0.5  # per-operator probability of swapping operands
EQY_TIMEOUT = 600  # wall-clock cap on the equivalence check

COMMUTATIVE_KINDS = frozenset(
    {
        pyslang.SyntaxKind.AddExpression,
        pyslang.SyntaxKind.MultiplyExpression,
        pyslang.SyntaxKind.BinaryAndExpression,
        pyslang.SyntaxKind.BinaryOrExpression,
        pyslang.SyntaxKind.BinaryXorExpression,
        pyslang.SyntaxKind.EqualityExpression,
        pyslang.SyntaxKind.InequalityExpression,
    }
)

PERMUTABLE_MEMBER_KINDS = frozenset(
    {
        pyslang.SyntaxKind.AlwaysBlock,
        pyslang.SyntaxKind.AlwaysCombBlock,
        pyslang.SyntaxKind.AlwaysFFBlock,
        pyslang.SyntaxKind.AlwaysLatchBlock,
        pyslang.SyntaxKind.InitialBlock,
        pyslang.SyntaxKind.FinalBlock,
        pyslang.SyntaxKind.ContinuousAssign,
        pyslang.SyntaxKind.HierarchyInstantiation,
    }
)


def _reflink_copy(src: Path, dst: Path) -> None:
    """Reflink-copy `src` directory tree to `dst`. `dst` must not exist."""
    proc = subprocess.run(
        ["cp", "-R", "--reflink=auto", str(src), str(dst)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"copy failed:\n{proc.stderr}")


def _resolve_include_dirs(design: DesignConfig) -> list[str]:
    """Include search paths, resolved against `design.root / design.rtl_dir`."""
    rtl_src = design.root / design.rtl_dir
    out = []
    for inc in design.include_dirs:
        path = (rtl_src / inc).resolve()
        if path.is_dir():
            out.append(str(path))
    return out


def _make_source_manager(include_dirs: list[str]) -> pyslang.SourceManager:
    """Fresh SourceManager with the given include directories."""
    sm = pyslang.SourceManager()
    for path in include_dirs:
        sm.addUserDirectories(path)
    return sm


def _error_signature(diags: pyslang.Diagnostics) -> Counter:
    """Multiset of diagnostic codes at error severity."""
    return Counter(str(d.code) for d in diags if d.isError())


def _count_kinds(root: pyslang.SyntaxNode, kinds: frozenset) -> int:
    total = 0

    def visit(node: pyslang.SyntaxNode) -> pyslang.VisitAction:
        nonlocal total
        if node.kind in kinds:
            total += 1
        return pyslang.VisitAction.Advance

    root.visit(visit)
    return total


def _per_file_rng(rel_path: Path, master_seed: int) -> random.Random:
    """Per-file deterministic RNG derived from relative path."""
    key = abs(master_seed).to_bytes(8, "little", signed=False)
    digest = hashlib.blake2b(
        rel_path.as_posix().encode("utf-8"), key=key, digest_size=8
    ).digest()
    return random.Random(int.from_bytes(digest, "little"))


def _transform_operand_swap(
    src: str,
    abs_path: Path,
    include_dirs: list[str],
    rng: random.Random,
) -> tuple[str, int]:
    """Swap operands around commutative binary operators."""
    sm = _make_source_manager(include_dirs)
    tree = pyslang.SyntaxTree.fromFileInMemory(src, sm, path=str(abs_path))
    orig_errors = _error_signature(tree.diagnostics)
    orig_count = _count_kinds(tree.root, COMMUTATIVE_KINDS)
    file_buffer = tree.root.sourceRange.start.buffer

    swaps: list[tuple[int, int, int, int]] = []  # (L_start, L_end, R_start, R_end)

    def visit(node: pyslang.SyntaxNode) -> pyslang.VisitAction:
        if node.kind not in COMMUTATIVE_KINDS:
            return pyslang.VisitAction.Advance
        binop = cast(pyslang.BinaryExpressionSyntax, node)
        left = binop.left
        right = binop.right
        if (
            node.sourceRange.start.buffer != file_buffer
            or left.sourceRange.start.buffer != file_buffer
            or left.sourceRange.end.buffer != file_buffer
            or right.sourceRange.start.buffer != file_buffer
            or right.sourceRange.end.buffer != file_buffer
        ):
            return pyslang.VisitAction.Advance
        l_start = left.sourceRange.start.offset
        l_end = left.sourceRange.end.offset
        r_start = right.sourceRange.start.offset
        r_end = right.sourceRange.end.offset
        if not (0 <= l_start < l_end <= r_start < r_end <= len(src)):
            return pyslang.VisitAction.Advance
        if "`" in src[l_end:r_start]:
            return pyslang.VisitAction.Advance
        if src[l_start:l_end].lstrip().startswith("'{"):
            return pyslang.VisitAction.Advance
        if src[r_start:r_end].lstrip().startswith("'{"):
            return pyslang.VisitAction.Advance
        if rng.random() < FLIP_PROBABILITY:
            swaps.append((l_start, l_end, r_start, r_end))
            # Operators nested inside a swapped one are not candidates.
            return pyslang.VisitAction.Skip
        return pyslang.VisitAction.Advance

    tree.root.visit(visit)

    if not swaps:
        return src, 0

    out = src
    for l_start, l_end, r_start, r_end in sorted(swaps, key=lambda t: -t[0]):
        new_chunk = out[r_start:r_end] + out[l_end:r_start] + out[l_start:l_end]
        out = out[:l_start] + new_chunk + out[r_end:]

    check_sm = _make_source_manager(include_dirs)
    check_tree = pyslang.SyntaxTree.fromFileInMemory(out, check_sm, path=str(abs_path))
    new_errors = _error_signature(check_tree.diagnostics)
    new_count = _count_kinds(check_tree.root, COMMUTATIVE_KINDS)
    if (new_errors - orig_errors) or new_count != orig_count:
        raise RuntimeError(
            f"operand_swap self-check failed for {abs_path}: "
            f"new_errors={sorted((new_errors - orig_errors).items())}, "
            f"orig_ops={orig_count}, new_ops={new_count}"
        )

    return out, len(swaps)


def _transform_decl_reorder(
    src: str,
    abs_path: Path,
    include_dirs: list[str],
    rng: random.Random,
) -> tuple[str, int]:
    """Permute the order of permutable module-body members (always blocks,
    initial/final blocks, continuous assigns, submodule instantiations)."""
    sm = _make_source_manager(include_dirs)
    tree = pyslang.SyntaxTree.fromFileInMemory(src, sm, path=str(abs_path))
    orig_errors = _error_signature(tree.diagnostics)
    orig_count = _count_kinds(tree.root, PERMUTABLE_MEMBER_KINDS)
    file_buffer = tree.root.sourceRange.start.buffer

    edits: list[tuple[int, int, str]] = []  # (old_start, old_end, replacement_text)
    moved = 0

    def visit(node: pyslang.SyntaxNode) -> pyslang.VisitAction:
        nonlocal moved
        if node.kind != pyslang.SyntaxKind.ModuleDeclaration:
            return pyslang.VisitAction.Advance
        mod = cast(pyslang.ModuleDeclarationSyntax, node)
        perm: list[pyslang.SyntaxNode] = []
        for m in mod.members:
            if (
                m.kind in PERMUTABLE_MEMBER_KINDS
                and m.sourceRange.start.buffer == file_buffer
                and m.sourceRange.end.buffer == file_buffer
            ):
                perm.append(m)
        if len(perm) < 2:
            return pyslang.VisitAction.Advance
        order = list(range(len(perm)))
        rng.shuffle(order)
        for slot, src_idx in enumerate(order):
            if slot == src_idx:
                continue
            old = perm[slot]
            new = perm[src_idx]
            edits.append(
                (
                    old.sourceRange.start.offset,
                    old.sourceRange.end.offset,
                    src[new.sourceRange.start.offset : new.sourceRange.end.offset],
                )
            )
            moved += 1
        return pyslang.VisitAction.Advance

    tree.root.visit(visit)

    if not edits:
        return src, 0

    out = src
    for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
        out = out[:start] + replacement + out[end:]

    check_sm = _make_source_manager(include_dirs)
    check_tree = pyslang.SyntaxTree.fromFileInMemory(out, check_sm, path=str(abs_path))
    new_errors = _error_signature(check_tree.diagnostics)
    new_count = _count_kinds(check_tree.root, PERMUTABLE_MEMBER_KINDS)
    if (new_errors - orig_errors) or new_count != orig_count:
        raise RuntimeError(
            f"decl_reorder self-check failed for {abs_path}: "
            f"new_errors={sorted((new_errors - orig_errors).items())}, "
            f"orig_members={orig_count}, new_members={new_count}"
        )

    return out, moved


TRANSFORMS: list[
    tuple[str, Callable[[str, Path, list[str], random.Random], tuple[str, int]]]
] = [
    ("operand_swap", _transform_operand_swap),
    ("decl_reorder", _transform_decl_reorder),
]


def _rewrite_text(
    src: str,
    abs_path: Path,
    include_dirs: list[str],
    rng: random.Random,
) -> tuple[str, dict[str, int]]:
    """Apply every transform in TRANSFORMS in sequence. Returns the final
    text plus a per-transform count of changes made."""
    counts: dict[str, int] = {}
    cur = src
    for name, transform in TRANSFORMS:
        cur, n = transform(cur, abs_path, include_dirs, rng)
        counts[name] = n
    return cur, counts


def _eqy_read_block(design: DesignConfig) -> str:
    """Yosys script fragment to read `design`'s RTL."""
    rtl_src = design.root / design.rtl_dir
    inc_args = "".join(f" -I {(rtl_src / d).resolve()}" for d in design.include_dirs)
    files = " ".join(str((rtl_src / f).resolve()) for f in design.rtl_files)
    return f"read -sv{inc_args} {files}\nprep -top {design.top_module}\n"


def verify_equivalence(
    gold: DesignConfig,
    gate: DesignConfig,
    workdir: Path,
) -> bool:
    """Run eqy to prove `gold` and `gate` are logically equivalent."""
    config = (
        "[options]\n\n"
        f"[gold]\n{_eqy_read_block(gold)}\n"
        f"[gate]\n{_eqy_read_block(gate)}\n"
        "[strategy simple]\nuse sat\ndepth 10\n"
    )
    workdir.parent.mkdir(parents=True, exist_ok=True)
    cfg_path = workdir.parent / f"{workdir.name}.eqy"
    cfg_path.write_text(config)
    try:
        proc = subprocess.run(
            [str(EQY_BIN), "-f", "-d", str(workdir), str(cfg_path)],
            capture_output=True,
            text=True,
            timeout=EQY_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"eqy timed out after {EQY_TIMEOUT}s", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(proc.stdout, file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
    return proc.returncode == 0


def rewrite_design(
    design: DesignConfig,
    output_dir: Path,
    seed: int = 0,
    verify: bool = False,
) -> DesignConfig:
    """Reflink-copy `design.root` to `output_dir`, rewrite each file in
    `design.rtl_files` in place and return a DesignConfig pointed at the copy."""
    if output_dir.exists():
        raise FileExistsError(output_dir)
    _reflink_copy(design.root, output_dir)
    include_dirs = _resolve_include_dirs(design)

    for rel in design.rtl_files:
        abs_orig = design.root / design.rtl_dir / rel
        abs_copy = output_dir / design.rtl_dir / rel
        src = abs_orig.read_text(encoding="utf-8")
        rng = _per_file_rng(rel, seed)
        new_text, counts = _rewrite_text(src, abs_orig, include_dirs, rng)
        if new_text != src:
            abs_copy.write_text(new_text, encoding="utf-8")

    rewritten = dataclasses.replace(design, root=output_dir)

    if verify:
        eqy_workdir = output_dir / "__eqy__"
        if not verify_equivalence(design, rewritten, eqy_workdir):
            raise RuntimeError(f"eqy: FAIL (workdir: {eqy_workdir})")

    return rewritten


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--design",
        required=True,
        help="Design name (resolved via common.designs.resolve_design).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Destination directory. Default: fresh mkdtemp().",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Master RNG seed. Same seed reproduces byte-identical output.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run eqy to prove the rewrite is logically equivalent.",
    )
    args = parser.parse_args()

    design = resolve_design(args.design)
    if args.output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="rtl_rewrite_")) / design.root.name
    else:
        output_dir = args.output_dir

    print(
        f"Rewriting {design.benchmark}/{design.name}/{design.variant} "
        f"-> {output_dir} (seed={args.seed})"
    )
    try:
        rewritten = rewrite_design(
            design, output_dir, seed=args.seed, verify=args.verify
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    print(f"Done. Rewritten root: {rewritten.root}")


if __name__ == "__main__":
    main()
