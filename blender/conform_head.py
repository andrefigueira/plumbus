"""Conform a rigged template head onto a generated head scan. Headless Blender.

THE SCAN IS THE MOULD, THE TEMPLATE IS THE ASSET. Generation observes the
subject but outputs one fused shell: the eyes are sculpted into the skull like
a statue's, and nothing can blink. The template carries the anatomy instead,
separate eyeballs and facial shape keys, and this script fits it to the scan:

  1. scale and centre the scan into the template's space
  2. shrinkwrap the template head onto the scan surface
  3. RE-BASE the shape keys: the wrapped shape becomes the new Basis while
     every expression key keeps its authored OFFSET. Blender refuses to apply
     a modifier over shape keys precisely because that would discard them, so
     the wrap is applied AS a key, its delta is added to every key including
     Basis (relative keys store absolute coords, so offsets ride along), and
     the scratch key is removed.
  4. carry the eyes along: each eye translates by the mean wrap-delta of the
     head vertices around it, so it lands inside the wrapped socket.

Contract with the template (stub or real): a mesh named Head whose first shape
key is Basis, and eye objects named Eye_*, origin at the eyeball centre.

Run:
  Blender --background --python conform_head.py -- \
      --scan scan.glb --template head.glb --out riggable.glb
"""
import argparse
import json
import sys

import bpy
from mathutils import Vector


def args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", required=True)
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--wrap-offset", type=float, default=0.002,
                    help="keep the template a hair outside the scan surface")
    ap.add_argument("--bake-size", type=int, default=2048,
                    help="albedo bake resolution; 0 skips the bake")
    ap.add_argument("--mode", choices=["conform", "transfer"], default="conform",
                    help="conform: the template becomes the asset (clean topology, "
                         "games). transfer: the template's expression deltas are "
                         "splatted onto the scan itself, which keeps every pixel "
                         "and triangle of the generated head (hero fidelity, film).")
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return ap.parse_args(argv)


def fail(msg: str) -> None:
    print(f"PLUMBUS_RESULT: {json.dumps({'ok': False, 'error': msg})}")
    sys.exit(1)


def world_bounds(objs):
    lo = Vector((1e9,) * 3)
    hi = Vector((-1e9,) * 3)
    for o in objs:
        for corner in o.bound_box:
            w = o.matrix_world @ Vector(corner)
            lo = Vector(map(min, lo, w))
            hi = Vector(map(max, hi, w))
    return lo, hi


def main() -> None:
    a = args()
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # ── template
    bpy.ops.import_scene.gltf(filepath=a.template)
    head = next((o for o in bpy.data.objects
                 if o.type == "MESH" and o.name.startswith("Head")
                 and o.data.shape_keys), None)
    if head is None:
        fail("template has no mesh named Head with shape keys")
    eyes = [o for o in bpy.data.objects if o.type == "MESH" and o.name.startswith("Eye")]
    template_objs = {head, *eyes}

    # ── scan, joined to one mesh so the wrap has a single target
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=a.scan)
    scan_meshes = [o for o in set(bpy.data.objects) - before if o.type == "MESH"]
    if not scan_meshes:
        fail("scan contained no mesh")
    for o in bpy.data.objects:
        o.select_set(o in scan_meshes)
    bpy.context.view_layer.objects.active = scan_meshes[0]
    if len(scan_meshes) > 1:
        bpy.ops.object.join()
    scan = bpy.context.active_object
    scan.name = "Scan"

    # ── put the scan where the template lives: same height, same centre
    t_lo, t_hi = world_bounds([head])
    s_lo, s_hi = world_bounds([scan])
    t_h = t_hi.z - t_lo.z
    s_h = s_hi.z - s_lo.z
    if s_h < 1e-9:
        fail("scan has no height")
    scale = t_h / s_h
    scan.scale = (scan.scale[0] * scale, scan.scale[1] * scale, scan.scale[2] * scale)
    bpy.context.view_layer.update()
    s_lo, s_hi = world_bounds([scan])
    scan.location += (t_lo + t_hi) / 2 - (s_lo + s_hi) / 2
    bpy.context.view_layer.update()

    hi = t_hi
    lo = t_lo

    # ── remember where the head's vertices start, in world space
    depsgraph = bpy.context.evaluated_depsgraph_get()
    basis_world = [head.matrix_world @ v.co for v in head.data.vertices]

    # ── wrap the head onto the scan, applied as a shape key
    for o in bpy.data.objects:
        o.select_set(o is head)
    bpy.context.view_layer.objects.active = head
    mod = head.modifiers.new(name="conform", type="SHRINKWRAP")
    mod.target = scan
    mod.wrap_method = "TARGET_PROJECT"
    mod.offset = a.wrap_offset
    bpy.ops.object.modifier_apply_as_shapekey(modifier="conform")

    keys = head.data.shape_keys.key_blocks
    wrap_key = keys[-1]                       # the key the modifier just wrote
    n = len(head.data.vertices)

    # ── re-base: wrapped shape becomes Basis, expression offsets survive
    deltas = [wrap_key.data[i].co - keys[0].data[i].co for i in range(n)]
    for key in keys:
        if key is wrap_key:
            continue
        for i in range(n):
            key.data[i].co += deltas[i]
    head.shape_key_remove(wrap_key)

    moved = sum(1 for d in deltas if d.length > 1e-6)
    mean_delta = sum((d.length for d in deltas), 0.0) / max(n, 1)

    # ── eyes follow their sockets
    eye_moves = {}
    for eye in eyes:
        centre = eye.matrix_world.translation
        near = [i for i, w in enumerate(basis_world) if (w - centre).length < 0.45]
        if not near:
            near = range(n)
        shift = sum((deltas[i] for i in near), Vector()) / len(near)
        eye.location += shift
        eye_moves[eye.name] = round(shift.length, 4)

    if a.mode == "transfer":
        # ── the scan keeps the looks, the template donates the movement.
        # Every expression key's delta field is splatted from the wrapped
        # template onto the scan's own vertices through a gaussian kernel over
        # a KD-tree, so the 197k-triangle head that already looks like the
        # reference gains blink_L / blink_R / jawOpen without losing a pixel.
        from mathutils.kdtree import KDTree

        expr_keys = [k for k in keys if k.name != "Basis"]
        # world-space positions of template verts that any expression moves
        idx_map = []
        for i in range(n):
            if any((k.data[i].co - keys[0].data[i].co).length > 1e-5 for k in expr_keys):
                idx_map.append(i)
        if not idx_map:
            fail("template has no expression deltas to transfer")
        head_mw = head.matrix_world
        tree = KDTree(len(idx_map))
        for j, i in enumerate(idx_map):
            tree.insert(head_mw @ keys[0].data[i].co, j)
        tree.balance()

        span = (hi - lo).length or 1.0
        # Tight kernel: expression detail is local. A wide radius drags the
        # nose and forehead along with the lids, which reads as face collapse.
        radius = span * 0.045
        sigma2 = (radius * 0.55) ** 2

        scan_me = scan.data
        if not scan_me.shape_keys:
            scan.shape_key_add(name="Basis", from_mix=False)
        scan_mw = scan.matrix_world
        scan_mw_inv = scan_mw.inverted()

        made = {}
        new_keys = {k.name: scan.shape_key_add(name=k.name, from_mix=False) for k in expr_keys}
        vcount = len(scan_me.vertices)
        touched = 0
        for vi in range(vcount):
            w_co = scan_mw @ scan_me.vertices[vi].co
            hits = [(co, j, d) for co, j, d in tree.find_n(w_co, 6) if d < radius]
            if not hits:
                continue
            touched += 1
            weights = []
            total = 0.0
            for _, j, d in hits:
                w = pow(2.718281828, -(d * d) / sigma2)
                weights.append((idx_map[j], w))
                total += w
            for k in expr_keys:
                delta = Vector()
                for i, w in weights:
                    delta += (head_mw @ k.data[i].co - head_mw @ keys[0].data[i].co) * w
                delta /= total
                nk = new_keys[k.name]
                nk.data[vi].co = scan_me.vertices[vi].co + scan_mw_inv.to_3x3() @ delta
        for name, nk in new_keys.items():
            made[name] = True

        # the scan IS the asset now; the template was only the donor
        for o in list(template_objs):
            bpy.data.objects.remove(o, do_unlink=True)
        for o in bpy.data.objects:
            o.select_set(o is scan)
        bpy.ops.export_scene.gltf(filepath=a.out, export_format="GLB",
                                  use_selection=True, export_morph=True,
                                  export_yup=True, export_apply=False)
        print("PLUMBUS_RESULT: " + json.dumps({
            "ok": True,
            "mode": "transfer",
            "scan_vertices": vcount,
            "vertices_given_motion": touched,
            "shape_keys": ["Basis", *made.keys()],
        }))
        return

    # ── carry the face across: bake the scan's albedo onto the template's UVs.
    # Selected-to-active diffuse bake, colour pass only, so no lighting is
    # cooked in. The template sits a hair off the scan surface, so rays get a
    # cage extrusion and a generous max distance to land in both directions.
    baked = False
    bake_error = None
    if a.bake_size:
        try:
            scene = bpy.context.scene
            scene.render.engine = "CYCLES"
            scene.cycles.device = "CPU"
            scene.cycles.samples = 4

            img = bpy.data.images.new("HeadAlbedo", a.bake_size, a.bake_size, alpha=False)
            mat = bpy.data.materials.new("HeadBaked")
            mat.use_nodes = True
            nt = mat.node_tree
            tex = nt.nodes.new("ShaderNodeTexImage")
            tex.image = img
            head.data.materials.clear()
            head.data.materials.append(mat)
            nt.nodes.active = tex
            tex.select = True

            for o in bpy.data.objects:
                o.select_set(False)
            scan.select_set(True)
            head.select_set(True)
            bpy.context.view_layer.objects.active = head

            span = (hi - lo).length if (hi - lo).length > 0 else 1.0
            bpy.ops.object.bake(
                type="DIFFUSE", pass_filter={"COLOR"},
                use_selected_to_active=True,
                cage_extrusion=span * 0.03,
                max_ray_distance=span * 0.3,
                margin=8,
            )
            bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
            nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
            bsdf.inputs["Roughness"].default_value = 0.62
            img.pack()
            baked = True
        except Exception as exc:  # noqa: BLE001
            # A failed bake must never cost the conform; ship white over nothing.
            bake_error = f"{exc.__class__.__name__}: {exc}"

    # ── the mould has done its job
    bpy.data.objects.remove(scan, do_unlink=True)

    for o in bpy.data.objects:
        o.select_set(o in template_objs)
    bpy.ops.export_scene.gltf(filepath=a.out, export_format="GLB",
                              use_selection=True, export_morph=True,
                              export_yup=True, export_apply=False)

    print("PLUMBUS_RESULT: " + json.dumps({
        "ok": True,
        "vertices": n,
        "vertices_moved": moved,
        "mean_wrap_delta": round(mean_delta, 4),
        "shape_keys": [k.name for k in keys if k.name],
        "eyes": eye_moves,
        "baked": baked,
        "bake_error": bake_error,
    }))


main()
