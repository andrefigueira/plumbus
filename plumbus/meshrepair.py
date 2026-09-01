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


def _rims_of(sub) -> list[list[int]]:
    """Split one boundary component into fillable rings.

    A clean rim is a simple cycle: every vertex has exactly two boundary
    neighbours, so the greedy walk is correct. Where the surface is non-manifold
    the component is several rings sharing vertices, and walking it greedily
    wanders off and returns a partial loop that fans into self-intersecting
    junk. Those get decomposed into cycles instead, which is why a skull full of
    non-manifold rims stays open under a plain walk.
    """
    import networkx as nx

    degrees = dict(sub.degree())
    if degrees and all(d == 2 for d in degrees.values()):
        loop = _walk_rim(sub)
        return [loop] if loop else []

    rims: list[list[int]] = []
    try:
        for cycle in nx.cycle_basis(sub):
            if len(cycle) >= 3:
                rims.append(cycle)
    except Exception:  # noqa: BLE001
        loop = _walk_rim(sub)
        if loop:
            rims.append(loop)
    return rims


def _fan_origin(loop: list[int], positions) -> int:
    """Index within `loop` of the vertex nearest its centroid.

    Fanning from an arbitrary vertex makes long slivers across a non-convex rim.
    Starting from the most central one keeps the new triangles far better shaped,
    without adding a vertex, which would have no UV and would break the texture.
    """
    pts = positions[loop]
    centre = pts.mean(axis=0)
    return int(np.argmin(((pts - centre) ** 2).sum(axis=1)))


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


def _face_labels(welded_faces: np.ndarray) -> np.ndarray:
    """Connected-component id per face, over welded topology.

    trimesh's own `split` builds the whole component meshes and was killed by
    the OS on a 200k-face character. This only needs the labels, so it goes
    straight to a sparse graph and never materialises a mesh per component.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    e = welded_faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    n = int(welded_faces.max()) + 1
    graph = coo_matrix(
        (np.ones(len(e), dtype=np.int8), (e[:, 0], e[:, 1])), shape=(n, n)
    )
    _, labels = connected_components(graph, directed=False)
    return labels[welded_faces[:, 0]]


def _drop_fragments(mesh, welded_faces: np.ndarray, min_body_frac: float) -> int:
    """Delete disconnected shells too small to be a real part. Returns faces removed.

    Generation strews stray shells around a subject: a few hundred triangles of
    hair wisp or fabric edge floating off the surface. They read as grit, they
    inflate the body count, and nothing downstream wants them. Size is measured
    against the whole mesh, so a legitimately separate part, a lantern handle or
    a scabbard, sits far above the threshold and survives.

    Only the face array is filtered. Vertices and their UVs are left exactly as
    they were, which is what keeps the texture intact.
    """
    if min_body_frac <= 0 or not len(mesh.faces):
        return 0
    try:
        labels = _face_labels(welded_faces)
    except Exception:  # noqa: BLE001
        return 0

    ids, counts = np.unique(labels, return_counts=True)
    if len(ids) < 2:
        return 0

    total = len(mesh.faces)
    keep_ids = ids[counts >= total * min_body_frac]
    if not len(keep_ids) or len(keep_ids) == len(ids):
        return 0

    mask = np.isin(labels, keep_ids)
    removed = int((~mask).sum())
    mesh.update_faces(mask)
    return removed


def repair(
    src: Path,
    dest: Path | None = None,
    max_hole_edges: int = 400,
    max_hole_frac: float = 0.12,
    min_body_frac: float = 0.0,
) -> dict[str, Any]:
    """Close boundary loops that are defects, keeping UVs and textures.

    Two ceilings, because a big opening is not the same thing as a lot of
    vertices. `max_hole_frac` is the real test: a rim whose bounding diagonal is
    under this fraction of the model's own diagonal is a defect and gets filled,
    however many vertices it has. That is what lets a skull close while the
    hollow under a coat, which is genuinely meant to be open, stays open.
    `max_hole_edges` is only a cost ceiling on the triangulation.

    `min_body_frac` optionally drops disconnected shells below that fraction of
    the mesh, which is what turns twenty-five floating fragments into one object.
    """
    import networkx as nx
    import trimesh

    dest = Path(dest or src)
    before = survey(src)

    mesh = trimesh.load(src, force="mesh")

    # Drop stray shells first, so their rims are not counted as holes to fill.
    dropped_faces = _drop_fragments(mesh, _weld(mesh)[0], min_body_frac)

    welded_faces, representative = _weld(mesh)
    positions = np.asarray(mesh.vertices)

    # Scale the "is this hole meant to be here" test to the model's own size.
    extent = positions.max(axis=0) - positions.min(axis=0)
    model_diag = float(np.linalg.norm(extent)) or 1.0

    # Boundary = an edge used by exactly one face, measured on welded topology.
    e = np.sort(welded_faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    uniq, counts = np.unique(e, axis=0, return_counts=True)
    boundary = uniq[counts == 1]

    added: list[list[int]] = []
    skipped_large = 0
    skipped_costly = 0
    rims_filled = 0

    if len(boundary):
        g = nx.from_edgelist([tuple(x) for x in boundary])
        # Each connected component of the boundary graph is one or more hole rims.
        # cycle_basis over the whole graph is wrong: it returns a *basis*, so rims
        # that share a vertex collapse into one entry and most holes never close.
        for comp in nx.connected_components(g):
            if len(comp) < 3:
                continue
            for loop in _rims_of(g.subgraph(comp)):
                if len(loop) < 3:
                    continue
                if len(loop) > max_hole_edges:
                    skipped_costly += 1
                    continue
                orig = [int(representative[v]) for v in loop]
                pts = positions[orig]
                span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
                if span > model_diag * max_hole_frac:
                    skipped_large += 1
                    continue
                o = _fan_origin(orig, positions)
                ring = orig[o:] + orig[:o]
                for i in range(1, len(ring) - 1):
                    added.append([ring[0], ring[i], ring[i + 1]])
                rims_filled += 1

    if added:
        mesh.faces = np.vstack([mesh.faces, np.array(added, dtype=mesh.faces.dtype)])

    mesh.export(dest)

    after = survey(dest)
    return {
        "before": before,
        "after": after,
        "faces_added": len(added),
        "faces_dropped": dropped_faces,
        "rims_filled": rims_filled,
        "holes_closed": (before.get("holes") or 0) - (after.get("holes") or 0),
        "skipped_large_openings": skipped_large,
        "skipped_costly_rims": skipped_costly,
    }
