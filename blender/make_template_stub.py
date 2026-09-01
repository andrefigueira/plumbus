"""Build the STUB conform template: proof plumbing, not anatomy.

The real template is an authored base head with proper eyelid topology and the
full ARKit shape set, and sourcing it is its own task. This stub exists so the
conform machinery can be built and tested end to end today: a head-ish ovoid
named Head with blink_L / blink_R / jawOpen shape keys, and two separate
eyeball spheres named Eye_L / Eye_R. Anything the pipeline does to this, it
will do to the real template unchanged, because the CONTRACT is the same:

    Head    mesh with shape keys, Basis first
    Eye_*   separate mesh objects, origin at the eyeball's centre

Run:  Blender --background --python make_template_stub.py -- --out template.glb
"""
import argparse
import math
import sys

import bpy


def args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return ap.parse_args(argv)


def main() -> None:
    a = args()
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Head: a sphere pulled into an ovoid. Enough vertices that a shrinkwrap
    # has something to move (subdivisions=4 -> ~2.5k verts).
    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=32, radius=1.0)
    head = bpy.context.active_object
    head.name = "Head"
    head.scale = (0.78, 0.92, 1.0)   # narrower, slightly deep, full height
    bpy.ops.object.transform_apply(scale=True)

    # Eyes: separate objects, origins at their centres, sitting in the face.
    eyes = []
    for name, x in (("Eye_L", 0.30), ("Eye_R", -0.30)):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16, radius=0.11,
                                             location=(x, -0.62, 0.25))
        eye = bpy.context.active_object
        eye.name = name
        eyes.append(eye)

    # Shape keys. Crude on purpose: they only need to survive the pipeline and
    # be visibly, countably present at the far end.
    bpy.context.view_layer.objects.active = head
    head.shape_key_add(name="Basis", from_mix=False)
    me = head.data

    def add_key(name, predicate, move):
        key = head.shape_key_add(name=name, from_mix=False)
        for i, v in enumerate(me.vertices):
            if predicate(v.co):
                key.data[i].co = v.co + move(v.co)

    from mathutils import Vector

    # blink: pull the brow band above each eye downward
    add_key("blink_L", lambda c: c.x > 0.12 and c.y < -0.3 and 0.28 < c.z < 0.62,
            lambda c: Vector((0, 0, -0.16)))
    add_key("blink_R", lambda c: c.x < -0.12 and c.y < -0.3 and 0.28 < c.z < 0.62,
            lambda c: Vector((0, 0, -0.16)))
    # jaw: drop the chin region
    add_key("jawOpen", lambda c: c.z < -0.45 and c.y < 0.1,
            lambda c: Vector((0, -0.05, -0.22)))

    for o in [head, *eyes]:
        o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=a.out, export_format="GLB",
                              use_selection=True, export_morph=True,
                              export_yup=True, export_apply=False)
    print(f"PLUMBUS_RESULT: wrote {a.out} keys={[k.name for k in me.shape_keys.key_blocks]}")


main()
