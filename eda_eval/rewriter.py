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
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pyslang

from common.config import DesignConfig
from common.designs import resolve_design

FLIP_PROBABILITY = 0.5  # per-operator probability of swapping operands

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

RENAME_PREFIX = "s_"  # prefix on renamed internal signals
RENAME_DIGEST_BYTES = 6  # 12 hex chars; collision-resistant up to ~16M names
RENAMABLE_DECL_KINDS = frozenset(
    {pyslang.SyntaxKind.DataDeclaration, pyslang.SyntaxKind.NetDeclaration}
)
IDENTIFIER_NAME_KINDS = frozenset(
    {pyslang.SyntaxKind.IdentifierName, pyslang.SyntaxKind.IdentifierSelectName}
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


def _directives_balanced(text: str) -> bool:
    """True iff `text` has well-formed `ifdef/`ifndef/`else/`elsif/`endif nesting."""
    DIRECTIVE_RE = re.compile(r"`(ifdef|ifndef|else|elsif|endif)\b")
    depth = 0
    for m in DIRECTIVE_RE.finditer(text):
        kw = m.group(1)
        if kw in ("ifdef", "ifndef"):
            depth += 1
        elif kw == "endif":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0:  # else/elsif outside any ifdef
            return False
    return depth == 0


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
        # Collect permutable members whose own text has balanced directives.
        candidates: list[pyslang.SyntaxNode] = []
        for m in mod.members:
            if (
                m.kind in PERMUTABLE_MEMBER_KINDS
                and m.sourceRange.start.buffer == file_buffer
                and m.sourceRange.end.buffer == file_buffer
            ):
                s, e = m.sourceRange.start.offset, m.sourceRange.end.offset
                if _directives_balanced(src[s:e]):
                    candidates.append(m)
        # Split into groups based on `ifdef/`endif to avoid swaps across this boundary.
        groups: list[list[pyslang.SyntaxNode]] = []
        current: list[pyslang.SyntaxNode] = []
        prev_end: int | None = None
        for m in candidates:
            s = m.sourceRange.start.offset
            if prev_end is not None and not _directives_balanced(src[prev_end:s]):
                if len(current) >= 2:
                    groups.append(current)
                current = []
            current.append(m)
            prev_end = m.sourceRange.end.offset
        if len(current) >= 2:
            groups.append(current)
        for group in groups:
            order = list(range(len(group)))
            rng.shuffle(order)
            for slot, src_idx in enumerate(order):
                if slot == src_idx:
                    continue
                old = group[slot]
                new = group[src_idx]
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


def _hash_name(name: str, key: bytes) -> str:
    """Deterministic short hash."""
    digest = hashlib.blake2b(
        name.encode("utf-8"), key=key, digest_size=RENAME_DIGEST_BYTES
    ).hexdigest()
    return f"{RENAME_PREFIX}{digest}"


def _transform_signal_rename(
    src: str,
    abs_path: Path,
    include_dirs: list[str],
    rng: random.Random,
) -> tuple[str, int]:
    """Rename module-internal signals to a hash-based scheme. Skips names that
    are shadowed inside generate/function/task scopes."""
    sm = _make_source_manager(include_dirs)
    tree = pyslang.SyntaxTree.fromFileInMemory(src, sm, path=str(abs_path))
    orig_errors = _error_signature(tree.diagnostics)
    orig_id_count = _count_kinds(tree.root, IDENTIFIER_NAME_KINDS)
    file_buffer = tree.root.sourceRange.start.buffer

    key = rng.getrandbits(64).to_bytes(8, "little", signed=False)
    edits: list[tuple[int, int, str]] = []

    def process_module(mod: pyslang.ModuleDeclarationSyntax) -> None:
        # Collect every Declarator name anywhere in this module subtree
        decl_counts: Counter = Counter()

        def count_visit(n: pyslang.SyntaxNode) -> pyslang.VisitAction:
            if n is not mod and n.kind == pyslang.SyntaxKind.ModuleDeclaration:
                return pyslang.VisitAction.Skip
            if n.kind == pyslang.SyntaxKind.Declarator:
                decl_counts[cast(pyslang.DeclaratorSyntax, n).name.valueText] += 1
            return pyslang.VisitAction.Advance

        mod.visit(count_visit)
        unshadowed = {name for name, c in decl_counts.items() if c == 1}

        # Port-related names from the header and any non-ANSI PortDeclarations
        # in the body.
        port_names: set[str] = set()

        def header_visit(n: pyslang.SyntaxNode) -> pyslang.VisitAction:
            if n.kind == pyslang.SyntaxKind.Declarator:
                port_names.add(cast(pyslang.DeclaratorSyntax, n).name.valueText)
            elif n.kind == pyslang.SyntaxKind.IdentifierName:
                ident = cast(pyslang.IdentifierNameSyntax, n)
                port_names.add(ident.identifier.valueText)
            return pyslang.VisitAction.Advance

        mod.header.visit(header_visit)
        for m in mod.members:
            if m.kind == pyslang.SyntaxKind.PortDeclaration:
                for d in cast(pyslang.PortDeclarationSyntax, m).declarators:
                    if isinstance(d, pyslang.DeclaratorSyntax):
                        port_names.add(d.name.valueText)

        # Module-body level variable/net declarations.
        mod_level: set[str] = set()
        for m in mod.members:
            if m.kind in RENAMABLE_DECL_KINDS:
                for d in m.declarators:
                    if isinstance(d, pyslang.DeclaratorSyntax):
                        mod_level.add(d.name.valueText)

        rename_set = (mod_level & unshadowed) - port_names
        if not rename_set:
            return

        rename_map = {name: _hash_name(name, key) for name in rename_set}
        if len(set(rename_map.values())) != len(rename_map):
            raise RuntimeError(f"hash collision in rename map for {abs_path}")

        def emit_rename(tok: pyslang.Token, new_name: str) -> None:
            r = tok.range
            if r.start.buffer != file_buffer or r.end.buffer != file_buffer:
                return
            edits.append((r.start.offset, r.end.offset, new_name))

        def rename_visit(n: pyslang.SyntaxNode) -> pyslang.VisitAction:
            if n is not mod and n.kind == pyslang.SyntaxKind.ModuleDeclaration:
                return pyslang.VisitAction.Skip
            if n.kind == pyslang.SyntaxKind.Declarator:
                tok = cast(pyslang.DeclaratorSyntax, n).name
                if tok.valueText in rename_map:
                    emit_rename(tok, rename_map[tok.valueText])
            elif n.kind == pyslang.SyntaxKind.IdentifierName:
                tok = cast(pyslang.IdentifierNameSyntax, n).identifier
                if tok.valueText in rename_map:
                    emit_rename(tok, rename_map[tok.valueText])
            elif n.kind == pyslang.SyntaxKind.IdentifierSelectName:
                tok = cast(pyslang.IdentifierSelectNameSyntax, n).identifier
                if tok.valueText in rename_map:
                    emit_rename(tok, rename_map[tok.valueText])
            return pyslang.VisitAction.Advance

        mod.visit(rename_visit)

    def outer_visit(node: pyslang.SyntaxNode) -> pyslang.VisitAction:
        if node.kind == pyslang.SyntaxKind.ModuleDeclaration:
            process_module(cast(pyslang.ModuleDeclarationSyntax, node))
            return pyslang.VisitAction.Skip
        return pyslang.VisitAction.Advance

    tree.root.visit(outer_visit)

    if not edits:
        return src, 0

    out = src
    for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
        out = out[:start] + replacement + out[end:]

    check_sm = _make_source_manager(include_dirs)
    check_tree = pyslang.SyntaxTree.fromFileInMemory(out, check_sm, path=str(abs_path))
    new_errors = _error_signature(check_tree.diagnostics)
    new_id_count = _count_kinds(check_tree.root, IDENTIFIER_NAME_KINDS)
    if (new_errors - orig_errors) or new_id_count != orig_id_count:
        raise RuntimeError(
            f"signal_rename self-check failed for {abs_path}: "
            f"new_errors={sorted((new_errors - orig_errors).items())}, "
            f"orig_ids={orig_id_count}, new_ids={new_id_count}"
        )

    return out, len(edits)


TransformFn = Callable[[str, Path, list[str], random.Random], tuple[str, int]]

TRANSFORMS: list[tuple[str, TransformFn]] = [
    ("operand_swap", _transform_operand_swap),
    ("decl_reorder", _transform_decl_reorder),
    ("signal_rename", _transform_signal_rename),
]


def _rewrite_text(
    src: str,
    abs_path: Path,
    include_dirs: list[str],
    rng: random.Random,
) -> tuple[str, dict[str, int]]:
    """Apply every transform in TRANSFORMS in sequence. Returns the final
    text and per-transform change counts."""
    counts: dict[str, int] = {}
    cur = src
    for name, transform in TRANSFORMS:
        cur, n = transform(cur, abs_path, include_dirs, rng)
        counts[name] = n
    return cur, counts


def rewrite_design(
    design: DesignConfig,
    output_dir: Path,
    seed: int = 0,
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
        # Skip files with non-ASCII characters to avoid pyslang offset misalignment.
        try:
            src = abs_orig.read_text(encoding="utf-8")
            assert all(ord(c) < 128 for c in src)
        except (UnicodeDecodeError, AssertionError):
            print(f"  {rel}: skipped (non-ASCII source)")
            continue
        rng = _per_file_rng(rel, seed)
        new_text, counts = _rewrite_text(src, abs_orig, include_dirs, rng)
        if new_text != src:
            abs_copy.write_text(new_text, encoding="utf-8")

    return dataclasses.replace(design, root=output_dir)


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
        rewritten = rewrite_design(design, output_dir, seed=args.seed)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    print(f"Done. Rewritten root: {rewritten.root}")


if __name__ == "__main__":
    main()
