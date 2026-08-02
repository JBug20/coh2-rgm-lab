from __future__ import annotations

import struct
import unittest

from coh2_rgm_lab.rgm import (
    geometry_payload_from_obj,
    obj_text,
    parse_geometry_payload,
    parse_obj,
    transform_obj_to_donor,
)


def _text(value: str) -> bytes:
    encoded = value.encode("ascii")
    return struct.pack("<I", len(encoded)) + encoded


def _sample_geometry() -> bytes:
    indices = (0, 1, 2)
    layout = ((0, 3, 4), (3, 3, 2), (8, 3, 2))
    vertices = []
    for position, normal_bgra, uv in (
        ((0.0, 0.0, 0.0), bytes((127, 127, 255, 0)), (0, 0)),
        ((1.0, 0.0, 0.0), bytes((127, 127, 255, 0)), (65535, 0)),
        ((0.0, 1.0, 0.0), bytes((127, 127, 255, 0)), (0, 65535)),
    ):
        vertices.append(
            struct.pack("<3f", *position)
            + normal_bgra
            + struct.pack("<2H", *uv)
        )
    return b"".join(
        [
            b"\0",
            struct.pack("<I", 1),
            struct.pack("<I3H3fB", len(indices), *indices, 0.5, 0.5, 0.0, 1),
            _text("triangle"),
            struct.pack("<I", len(layout)),
            b"".join(struct.pack("<3I", *item) for item in layout),
            struct.pack("<2I", 3, 20),
            b"".join(vertices),
            struct.pack("<I", 3),
            _text("test_material"),
            struct.pack("<I", 0),
            b"\0\0\0\0\1",
        ]
    )


class RGMGeometryTests(unittest.TestCase):
    def test_parse_and_export_triangle(self) -> None:
        geometry = parse_geometry_payload(_sample_geometry())
        self.assertEqual(geometry.vertex_count, 3)
        self.assertEqual(geometry.vertex_stride, 20)
        self.assertEqual(geometry.sections[0].triangle_count, 1)
        self.assertEqual(geometry.position(1), (1.0, 0.0, 0.0))
        self.assertEqual(geometry.normal(0), (1.0, 0.0, 0.0))
        self.assertEqual(geometry.texcoord0(2), (0.0, 1.0))

        output = obj_text((geometry,))
        self.assertIn("o triangle", output)
        self.assertIn("usemtl test_material", output)
        self.assertIn("f 1/1/1 2/2/2 3/3/3", output)

    def test_obj_import_and_donor_payload_rebuild(self) -> None:
        donor = parse_geometry_payload(_sample_geometry())
        mesh = parse_obj(
            """
            o replacement
            v -1 0 -1
            v  1 0 -1
            v  1 0  1
            v -1 0  1
            vt 0 0
            vt 1 0
            vt 1 1
            vt 0 1
            vn 0 1 0
            f 1/1/1 2/2/1 3/3/1 4/4/1
            """
        )
        self.assertEqual(len(mesh.vertices), 4)
        self.assertEqual(mesh.triangle_count, 2)

        rebuilt = parse_geometry_payload(geometry_payload_from_obj(donor, mesh))
        self.assertEqual(rebuilt.vertex_count, 4)
        self.assertEqual(rebuilt.sections[0].triangle_count, 2)
        self.assertEqual(rebuilt.sections[0].name, "triangle")
        self.assertEqual(rebuilt.material, donor.material)
        self.assertEqual(rebuilt.bones, donor.bones)

    def test_obj_import_generates_missing_normals(self) -> None:
        mesh = parse_obj("v 0 0 0\nv 1 0 0\nv 0 0 1\nf 1 2 3\n")
        self.assertEqual(mesh.vertices[0].normal, (0.0, -1.0, 0.0))

    def test_obj_rotation_maps_x_length_to_z(self) -> None:
        donor = parse_geometry_payload(_sample_geometry())
        mesh = parse_obj("v 1 0 0\nv 2 0 0\nv 1 1 0\nf 1 2 3\n")
        rotated = transform_obj_to_donor(
            mesh, donor, fit=False, rotate=(0.0, -90.0, 0.0)
        )
        self.assertAlmostEqual(rotated.vertices[0].position[0], 0.0, places=6)
        self.assertAlmostEqual(rotated.vertices[0].position[2], 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
