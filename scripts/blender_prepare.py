"""Prepare the open Blender scene as a CoH2-friendly triangulated OBJ.

Run through Blender, not ordinary Python:
    blender --background model.blend --python scripts/blender_prepare.py -- \
        --output prepared.obj --triangles 12000
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import bpy


def arguments() -> argparse.Namespace:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--triangles", type=int, default=12_000)
    return parser.parse_args(values)


def triangle_count(objects: list[bpy.types.Object]) -> int:
    total = 0
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            mesh.calc_loop_triangles()
            total += len(mesh.loop_triangles)
        finally:
            evaluated.to_mesh_clear()
    return total


def main() -> None:
    args = arguments()
    if args.triangles < 100:
        raise SystemExit("--triangles must be at least 100")

    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise SystemExit("scene contains no mesh objects")

    before = triangle_count(meshes)
    ratio = min(1.0, args.triangles / before) if before else 1.0
    for obj in meshes:
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        if ratio < 0.9999:
            modifier = obj.modifiers.new(name="CoH2_Decimate", type="DECIMATE")
            modifier.ratio = ratio
            modifier.use_collapse_triangulate = True
            bpy.ops.object.modifier_apply(modifier=modifier.name)
        triangulate = obj.modifiers.new(name="CoH2_Triangulate", type="TRIANGULATE")
        bpy.ops.object.modifier_apply(modifier=triangulate.name)
        obj.select_set(False)

    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(bpy.ops.wm, "obj_export"):
        bpy.ops.wm.obj_export(
            filepath=str(output),
            export_selected_objects=True,
            export_materials=False,
            apply_modifiers=True,
            forward_axis="NEGATIVE_Z",
            up_axis="Y",
        )
    else:
        bpy.ops.export_scene.obj(
            filepath=str(output),
            use_selection=True,
            use_mesh_modifiers=True,
            use_materials=False,
            axis_forward="-Z",
            axis_up="Y",
        )

    after = triangle_count(meshes)
    print(f"CoH2 prep: {len(meshes)} mesh object(s), {before} -> {after} triangles")
    print(f"CoH2 prep: wrote {output}")


if __name__ == "__main__":
    main()
