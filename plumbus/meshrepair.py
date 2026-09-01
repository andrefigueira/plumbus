"""Close the pinholes generation leaves behind.

TRELLIS marches a surface out of a sparse voxel field. Where the field is thin
or ambiguous (fabric edges, hat brims, hair) it drops individual triangles,
leaving hundreds of tiny boundary loops. Each renders as a black speck, and
across a torso that reads as the model being see-through.

The awkward part is that a textured GLB duplicates vertices along every UV seam,
so measured naively the mesh looks shattered and every seam looks like a hole.
Fill those and you destroy the model. So the work happens on a welded copy, and
the new faces are mapped back onto the original vertex indices, which keeps the
UVs and therefore the textures intact.

CPU only, any platform. No CUDA, no gated weights.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np


def _weld(mesh):
    """Map UV-split vertices onto shared positions.

    Returns (welded_faces, representative) where `representative[w]` is an index
    into the ORIGINAL vertex array for welded vertex `w`. Going back through that
    array is what preserves UVs.
    """
    import trimesh

    # Quantise before grouping: float noise would leave seams unwelded.
    quantised = np.round(mesh.vertices, decimals=6)
    unique, inverse = trimesh.grouping.unique_rows(quantised)
    lookup = np.zeros(len(unique), dtype=np.int64)
    lookup[inverse[unique]] = unique  # identity seed
    # inverse maps original -> group id; build group id -> first original index
    order = np.argsort(inverse, kind="stable")
    first_seen = np.zeros(inverse.max() + 1, dtype=np.int64)
    first_seen[inverse[order]] = order
    welded_faces = inverse[mesh.faces]
    return welded_faces, first_seen


def _walk_rim(sub) -> list[int] | None:
    """Order a boundary component into a traversal. None if it is not a clean rim."""
    nodes = list(sub.nodes)
    start = nodes[0]
    loop = [start]
    seen = {start}
    cur = start
    while True:
        nxt = [n for n in sub.neighbors(cur) if n not in seen]
        if not nxt:
            break
        cur = nxt[0]
        loop.append(cur)
        seen.add(cur)
    return loop if len(loop) >= 3 else None


def survey(glb: Path) -> dict[str, Any]:
    """Topology as it really is, after welding vertices split for UV seams."""
    import trimesh

    m = trimesh.load(glb, force="mesh")
    welded = m.copy()
    # merge_tex/merge_norm collapse the UV-seam duplicates. Without them every
    # seam counts as a boundary and the numbers are fiction.
    welded.merge_vertices(merge_tex=True, merge_norm=True)

    _, counts = np.unique(welded.edges_sorted, axis=0, return_counts=True)
    out = {
        "vertices": len(m.vertices),
        "faces": len(m.faces),
        "welded_vertices": len(welded.vertices),
        "boundary_edges": int((counts == 1).sum()),
        "nonmanifold_edges": int((counts > 2).sum()),
        "bodies": int(welded.body_count),
        "watertight": bool(welded.is_watertight),
    }
    try:
        out["holes"] = len(welded.outline().entities)
    except Exception:  # noqa: BLE001
        out["holes"] = None
    return out


def repair(src: Path, dest: Path | None = None, max_hole_edges: int = 60) -> dict[str, Any]:
    """Close small boundary loops, keeping UVs and textures.

    `max_hole_edges` is a deliberate ceiling. A genuinely large opening (the
    hollow under a coat, the inside of a hat) is meant to be open, and bridging
    it would weld geometry that should stay separate. Anything larger is left
    alone and reported.
    """
    import networkx as nx
    import trimesh

    dest = Path(dest or src)
    before = survey(src)

    mesh = trimesh.load(src, force="mesh")
    welded_faces, representative = _weld(mesh)

    # Boundary = an edge used by exactly one face, measured on welded topology.
    e = np.sort(welded_faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    boundary = uniq[counts == 1]

    added: list[list[int]] = []
    skipped_large = 0

    if len(boundary):
        g = nx.from_edgelist([tuple(x) for x in boundary])
        # Each connected component of the boundary graph is one hole rim.
        # cycle_basis is wrong here: it returns a *basis*, so rims that share a
        # vertex collapse into one entry and most holes never get closed.
        for comp in nx.connected_components(g):
            sub = g.subgraph(comp)
            if len(comp) < 3:
                continue
            if len(comp) > max_hole_edges:
                skipped_large += 1
                continue
            # Walk the rim in order so the fan does not self-intersect.
            loop = _walk_rim(sub)
            if loop is None or len(loop) < 3:
                continue
            orig = [int(representative[v]) for v in loop]
            for i in range(1, len(orig) - 1):
                added.append([orig[0], orig[i], orig[i + 1]])

    if added:
        mesh.faces = np.vstack([mesh.faces, np.array(added, dtype=mesh.faces.dtype)])

    mesh.export(dest)

    after = survey(dest)
    return {
        "before": before,
        "after": after,
        "faces_added": len(added),
        "holes_closed": (before.get("holes") or 0) - (after.get("holes") or 0),
        "skipped_large_openings": skipped_large,
    }
