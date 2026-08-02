"""Geometry decoding, OBJ interchange, and donor-RGM rebuilding."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import struct
from collections import OrderedDict

from .chunky import Chunk, ChunkyFile


class RGMParseError(ValueError):
    """Raised when a generic-RGM geometry block cannot be decoded safely."""


SEMANTICS = {
    0: "POSITION",
    1: "BLENDINDICES",
    2: "BLENDWEIGHT",
    3: "NORMAL",
    4: "BINORMAL",
    5: "TANGENT",
    6: "COLOR",
    8: "TEXCOORD0",
    9: "TEXCOORD1",
    14: "TEXCOORD9",
}

# Sizes observed in the public RGM definition. Format 2 is four packed bytes;
# CoH2 interprets it according to the semantic (B8G8R8A8 for vectors and two
# little-endian UNORM16 values for texture coordinates).
FORMAT_SIZES = {2: 4, 3: 8, 4: 12, 13: 4}


@dataclass(frozen=True, slots=True)
class MeshSection:
    name: str
    indices: tuple[int, ...]
    center: tuple[float, float, float]
    flag: int

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3


@dataclass(frozen=True, slots=True)
class InputElement:
    semantic: int
    unknown: int
    format_code: int
    offset: int
    size: int

    @property
    def name(self) -> str:
        return SEMANTICS.get(self.semantic, f"SEMANTIC_{self.semantic}")


@dataclass(frozen=True, slots=True)
class GeometryBone:
    name: str
    matrix: tuple[float, ...]
    inverse_matrix: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class RGMGeometry:
    sections: tuple[MeshSection, ...]
    layout: tuple[InputElement, ...]
    vertex_count: int
    vertex_stride: int
    vertex_buffer: bytes
    post_vertex_unknown: int
    material: str
    bones: tuple[GeometryBone, ...]
    tail: bytes

    def element(self, semantic: int) -> InputElement | None:
        return next((item for item in self.layout if item.semantic == semantic), None)

    def _field(self, vertex: int, element: InputElement) -> bytes:
        if not 0 <= vertex < self.vertex_count:
            raise IndexError(vertex)
        start = vertex * self.vertex_stride + element.offset
        return self.vertex_buffer[start : start + element.size]

    def position(self, vertex: int) -> tuple[float, float, float]:
        element = self.element(0)
        if element is None or element.format_code != 4:
            raise RGMParseError("POSITION is not R32G32B32_FLOAT")
        return struct.unpack("<3f", self._field(vertex, element))

    def normal(self, vertex: int) -> tuple[float, float, float] | None:
        element = self.element(3)
        if element is None:
            return None
        if element.format_code != 2:
            raise RGMParseError("unsupported NORMAL encoding")
        packed = self._field(vertex, element)
        # DXGI B8G8R8A8 stores logical XYZ in byte order R=2, G=1, B=0.
        vector = tuple(
            max(-1.0, min(1.0, (packed[index] - 127.0) / 127.0))
            for index in (2, 1, 0)
        )
        length = math.sqrt(sum(component * component for component in vector))
        if length == 0:
            return (0.0, 0.0, 0.0)
        return tuple(component / length for component in vector)

    def texcoord0(self, vertex: int) -> tuple[float, float] | None:
        element = self.element(8)
        if element is None:
            return None
        field = self._field(vertex, element)
        if element.format_code == 2:
            u, v = struct.unpack("<2H", field)
            return u / 65535.0, v / 65535.0
        if element.format_code == 3:
            return struct.unpack("<2f", field)
        raise RGMParseError("unsupported TEXCOORD0 encoding")


@dataclass(frozen=True, slots=True)
class ObjVertex:
    position: tuple[float, float, float]
    normal: tuple[float, float, float]
    texcoord: tuple[float, float]


@dataclass(frozen=True, slots=True)
class ObjSection:
    name: str
    indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ObjMesh:
    vertices: tuple[ObjVertex, ...]
    sections: tuple[ObjSection, ...]

    @property
    def triangle_count(self) -> int:
        return sum(len(section.indices) // 3 for section in self.sections)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def take(self, size: int, label: str) -> bytes:
        if size < 0 or size > self.remaining:
            raise RGMParseError(
                f"{label}: need {size} byte(s) at 0x{self.offset:x}, "
                f"only {self.remaining} remain"
            )
        start = self.offset
        self.offset += size
        return self.data[start : start + size]

    def u8(self, label: str) -> int:
        return self.take(1, label)[0]

    def u32(self, label: str) -> int:
        return struct.unpack("<I", self.take(4, label))[0]

    def f32s(self, count: int, label: str) -> tuple[float, ...]:
        return struct.unpack(f"<{count}f", self.take(count * 4, label))

    def text(self, label: str) -> str:
        size = self.u32(f"{label} length")
        try:
            return self.take(size, label).decode("ascii")
        except UnicodeDecodeError as exc:
            raise RGMParseError(f"{label} is not ASCII") from exc


def parse_geometry_payload(payload: bytes) -> RGMGeometry:
    """Parse one documented ``FOLD/MRGM v3 > DATA/DATA v8`` payload."""

    reader = _Reader(payload)
    if reader.u8("geometry marker") != 0:
        raise RGMParseError("geometry marker is not zero")
    object_count = reader.u32("object count")
    if object_count > 10_000:
        raise RGMParseError(f"implausible object count: {object_count}")

    sections: list[MeshSection] = []
    for object_index in range(object_count):
        index_count = reader.u32(f"object {object_index} index count")
        if index_count > reader.remaining // 2:
            raise RGMParseError(f"object {object_index} index buffer is truncated")
        index_data = reader.take(index_count * 2, f"object {object_index} indices")
        indices = struct.unpack(f"<{index_count}H", index_data)
        center = reader.f32s(3, f"object {object_index} center")
        flag = reader.u8(f"object {object_index} flag")
        name = reader.text(f"object {object_index} name")
        if index_count % 3:
            raise RGMParseError(f"object {name!r} index count is not divisible by 3")
        sections.append(MeshSection(name, indices, center, flag))

    layout_count = reader.u32("input-layout count")
    if not 1 <= layout_count <= 64:
        raise RGMParseError(f"implausible input-layout count: {layout_count}")
    layout: list[InputElement] = []
    field_offset = 0
    for index in range(layout_count):
        semantic = reader.u32(f"layout {index} semantic")
        unknown = reader.u32(f"layout {index} unknown")
        format_code = reader.u32(f"layout {index} format")
        try:
            size = FORMAT_SIZES[format_code]
        except KeyError as exc:
            raise RGMParseError(f"unsupported vertex format code: {format_code}") from exc
        layout.append(InputElement(semantic, unknown, format_code, field_offset, size))
        field_offset += size

    vertex_count = reader.u32("vertex count")
    vertex_stride = reader.u32("vertex stride")
    if vertex_stride != field_offset:
        raise RGMParseError(
            f"vertex stride {vertex_stride} does not match layout size {field_offset}"
        )
    vertex_buffer = reader.take(vertex_count * vertex_stride, "vertex buffer")
    for section in sections:
        if section.indices and max(section.indices) >= vertex_count:
            raise RGMParseError(f"object {section.name!r} references a missing vertex")

    post_vertex_unknown = reader.u32("post-vertex value")
    material = reader.text("material name")
    bone_count = reader.u32("geometry bone count")
    if bone_count > 4096:
        raise RGMParseError(f"implausible geometry bone count: {bone_count}")
    bones = []
    for index in range(bone_count):
        matrix = reader.f32s(12, f"bone {index} matrix")
        inverse = reader.f32s(12, f"bone {index} inverse matrix")
        name = reader.text(f"bone {index} name")
        bones.append(GeometryBone(name, matrix, inverse))

    tail = reader.take(5, "geometry trailer")
    if reader.remaining:
        raise RGMParseError(f"{reader.remaining} unconsumed geometry byte(s)")
    return RGMGeometry(
        tuple(sections),
        tuple(layout),
        vertex_count,
        vertex_stride,
        vertex_buffer,
        post_vertex_unknown,
        material,
        tuple(bones),
        tail,
    )


def geometries(chunky: ChunkyFile) -> tuple[RGMGeometry, ...]:
    """Return every fully validated generic geometry block in *chunky*."""

    result = []
    errors = []
    for _depth, chunk in chunky.walk():
        if chunk.tag != "FOLD/MRGM" or chunk.children is None:
            continue
        for child in chunk.children:
            if child.tag != "DATA/DATA" or child.version != 8:
                continue
            try:
                result.append(parse_geometry_payload(child.payload))
            except RGMParseError as exc:
                errors.append((child.offset, exc))
    if not result and errors:
        details = "; ".join(f"0x{offset:x}: {error}" for offset, error in errors)
        raise RGMParseError(f"no valid geometry block found ({details})")
    if not result:
        raise RGMParseError("no FOLD/MRGM v3 geometry block found")
    return tuple(result)


def obj_text(items: tuple[RGMGeometry, ...], *, flip_v: bool = True) -> str:
    """Build a Wavefront OBJ representation of decoded generic geometry."""

    lines = ["# Exported by CoH2 RGM Lab", "s 1"]
    vertex_base = texcoord_base = normal_base = 1
    for geometry_index, geometry in enumerate(items):
        has_uv = geometry.element(8) is not None
        has_normal = geometry.element(3) is not None
        lines.append(f"# geometry {geometry_index}: {geometry.material}")
        for index in range(geometry.vertex_count):
            x, y, z = geometry.position(index)
            lines.append(f"v {x:.9g} {y:.9g} {z:.9g}")
        if has_uv:
            for index in range(geometry.vertex_count):
                uv = geometry.texcoord0(index)
                assert uv is not None
                u, v = uv
                lines.append(f"vt {u:.9g} {(1.0 - v if flip_v else v):.9g}")
        if has_normal:
            for index in range(geometry.vertex_count):
                normal = geometry.normal(index)
                assert normal is not None
                lines.append(f"vn {normal[0]:.9g} {normal[1]:.9g} {normal[2]:.9g}")

        lines.append(f"usemtl {geometry.material}")
        for section in geometry.sections:
            safe_name = section.name.replace(" ", "_")
            lines.append(f"o {safe_name}")
            for offset in range(0, len(section.indices), 3):
                refs = []
                for raw_index in section.indices[offset : offset + 3]:
                    position = vertex_base + raw_index
                    if has_uv and has_normal:
                        refs.append(
                            f"{position}/{texcoord_base + raw_index}/{normal_base + raw_index}"
                        )
                    elif has_uv:
                        refs.append(f"{position}/{texcoord_base + raw_index}")
                    elif has_normal:
                        refs.append(f"{position}//{normal_base + raw_index}")
                    else:
                        refs.append(str(position))
                lines.append("f " + " ".join(refs))
        vertex_base += geometry.vertex_count
        if has_uv:
            texcoord_base += geometry.vertex_count
        if has_normal:
            normal_base += geometry.vertex_count
    return "\n".join(lines) + "\n"


def export_obj(chunky: ChunkyFile, output: Path, *, flip_v: bool = True) -> tuple[RGMGeometry, ...]:
    items = geometries(chunky)
    output.write_text(obj_text(items, flip_v=flip_v), encoding="utf-8")
    return items


def _normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(component * component for component in vector))
    if length < 1e-20:
        return (0.0, 1.0, 0.0)
    return tuple(component / length for component in vector)


def _cross(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _sub(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return tuple(left[index] - right[index] for index in range(3))


def _add_in_place(target: list[float], value: tuple[float, float, float]) -> None:
    for index in range(3):
        target[index] += value[index]


def _obj_index(raw: str, size: int, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise RGMParseError(f"OBJ {label} index is not an integer: {raw!r}") from exc
    if value == 0:
        raise RGMParseError(f"OBJ {label} indices are one-based; zero is invalid")
    result = value - 1 if value > 0 else size + value
    if not 0 <= result < size:
        raise RGMParseError(f"OBJ {label} index {value} is out of range")
    return result


def parse_obj(text: str) -> ObjMesh:
    """Parse the Wavefront subset emitted by Blender, triangulating polygons."""

    positions: list[tuple[float, float, float]] = []
    texcoords: list[tuple[float, float]] = []
    normals: list[tuple[float, float, float]] = []
    vertices: list[list[object]] = []
    vertex_map: dict[tuple[int, int | None, int | None], int] = {}
    section_faces: OrderedDict[str, list[int]] = OrderedDict()
    current_name = "mesh"
    section_faces[current_name] = []

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        keyword = fields[0]
        try:
            if keyword == "v":
                if len(fields) < 4:
                    raise RGMParseError("OBJ vertex requires three coordinates")
                positions.append(tuple(float(value) for value in fields[1:4]))
            elif keyword == "vt":
                if len(fields) < 3:
                    raise RGMParseError("OBJ texture coordinate requires two values")
                texcoords.append((float(fields[1]), float(fields[2])))
            elif keyword == "vn":
                if len(fields) < 4:
                    raise RGMParseError("OBJ normal requires three coordinates")
                normals.append(_normalize(tuple(float(value) for value in fields[1:4])))
            elif keyword in ("o", "g"):
                name = "_".join(fields[1:]).strip() if len(fields) > 1 else "mesh"
                current_name = name or "mesh"
                section_faces.setdefault(current_name, [])
            elif keyword == "f":
                if len(fields) < 4:
                    raise RGMParseError("OBJ face requires at least three vertices")
                polygon = []
                for reference in fields[1:]:
                    parts = reference.split("/")
                    position_index = _obj_index(parts[0], len(positions), "position")
                    texcoord_index = (
                        _obj_index(parts[1], len(texcoords), "texture")
                        if len(parts) > 1 and parts[1]
                        else None
                    )
                    normal_index = (
                        _obj_index(parts[2], len(normals), "normal")
                        if len(parts) > 2 and parts[2]
                        else None
                    )
                    key = (position_index, texcoord_index, normal_index)
                    vertex_index = vertex_map.get(key)
                    if vertex_index is None:
                        vertex_index = len(vertices)
                        vertex_map[key] = vertex_index
                        vertices.append(
                            [
                                positions[position_index],
                                normals[normal_index] if normal_index is not None else None,
                                texcoords[texcoord_index]
                                if texcoord_index is not None
                                else (0.0, 0.0),
                            ]
                        )
                    polygon.append(vertex_index)
                target = section_faces[current_name]
                for index in range(1, len(polygon) - 1):
                    target.extend((polygon[0], polygon[index], polygon[index + 1]))
        except (ValueError, RGMParseError) as exc:
            if isinstance(exc, RGMParseError):
                raise RGMParseError(f"OBJ line {line_number}: {exc}") from exc
            raise RGMParseError(f"OBJ line {line_number}: invalid number") from exc

    if not vertices:
        raise RGMParseError("OBJ contains no faces")
    if len(vertices) > 65_535:
        raise RGMParseError(
            f"OBJ expands to {len(vertices)} vertices; RGM indices allow at most 65535"
        )

    missing = [vertex[1] is None for vertex in vertices]
    accumulated = [[0.0, 0.0, 0.0] for _ in vertices]
    for indices in section_faces.values():
        for offset in range(0, len(indices), 3):
            a, b, c = indices[offset : offset + 3]
            edge1 = _sub(vertices[b][0], vertices[a][0])
            edge2 = _sub(vertices[c][0], vertices[a][0])
            face_normal = _cross(edge1, edge2)
            for vertex_index in (a, b, c):
                if missing[vertex_index]:
                    _add_in_place(accumulated[vertex_index], face_normal)
    for index, is_missing in enumerate(missing):
        if is_missing:
            vertices[index][1] = _normalize(tuple(accumulated[index]))

    sections = tuple(
        ObjSection(name, tuple(indices))
        for name, indices in section_faces.items()
        if indices
    )
    return ObjMesh(
        tuple(ObjVertex(vertex[0], vertex[1], vertex[2]) for vertex in vertices),
        sections,
    )


def transform_obj_to_donor(
    mesh: ObjMesh,
    donor: RGMGeometry,
    *,
    fit: bool,
    scale: float = 1.0,
    translate: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotate: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> ObjMesh:
    """Apply a uniform transform, optionally centering/fitting to donor bounds."""

    def rotate_vector(vector: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = vector
        rx, ry, rz = (math.radians(value) for value in rotate)
        cosine, sine = math.cos(rx), math.sin(rx)
        y, z = y * cosine - z * sine, y * sine + z * cosine
        cosine, sine = math.cos(ry), math.sin(ry)
        x, z = x * cosine + z * sine, -x * sine + z * cosine
        cosine, sine = math.cos(rz), math.sin(rz)
        x, y = x * cosine - y * sine, x * sine + y * cosine
        return (x, y, z)

    rotated = tuple(
        ObjVertex(
            rotate_vector(vertex.position),
            _normalize(rotate_vector(vertex.normal)),
            vertex.texcoord,
        )
        for vertex in mesh.vertices
    )
    positions = [vertex.position for vertex in rotated]
    source_min = tuple(min(point[axis] for point in positions) for axis in range(3))
    source_max = tuple(max(point[axis] for point in positions) for axis in range(3))
    source_center = tuple((source_min[axis] + source_max[axis]) * 0.5 for axis in range(3))
    factor = scale
    target_center = (0.0, 0.0, 0.0)
    if fit:
        donor_positions = [donor.position(index) for index in range(donor.vertex_count)]
        donor_min = tuple(min(point[axis] for point in donor_positions) for axis in range(3))
        donor_max = tuple(max(point[axis] for point in donor_positions) for axis in range(3))
        target_center = tuple((donor_min[axis] + donor_max[axis]) * 0.5 for axis in range(3))
        source_extent = max(source_max[axis] - source_min[axis] for axis in range(3))
        donor_extent = max(donor_max[axis] - donor_min[axis] for axis in range(3))
        if source_extent <= 1e-20:
            raise RGMParseError("OBJ has zero-size bounds")
        factor *= donor_extent / source_extent
    else:
        source_center = (0.0, 0.0, 0.0)

    output = []
    for vertex in rotated:
        position = tuple(
            (vertex.position[axis] - source_center[axis]) * factor
            + target_center[axis]
            + translate[axis]
            for axis in range(3)
        )
        output.append(ObjVertex(position, vertex.normal, vertex.texcoord))
    return ObjMesh(tuple(output), mesh.sections)


def _pack_vector(vector: tuple[float, float, float]) -> bytes:
    normalized = _normalize(vector)
    logical = [max(0, min(255, round(component * 127.0 + 127.0))) for component in normalized]
    return bytes((logical[2], logical[1], logical[0], 0))


def _tangent_basis(mesh: ObjMesh) -> tuple[tuple[tuple[float, float, float], tuple[float, float, float]], ...]:
    tangents = [[0.0, 0.0, 0.0] for _ in mesh.vertices]
    for section in mesh.sections:
        for offset in range(0, len(section.indices), 3):
            a, b, c = section.indices[offset : offset + 3]
            va, vb, vc = (mesh.vertices[index] for index in (a, b, c))
            edge1 = _sub(vb.position, va.position)
            edge2 = _sub(vc.position, va.position)
            du1 = vb.texcoord[0] - va.texcoord[0]
            dv1 = vb.texcoord[1] - va.texcoord[1]
            du2 = vc.texcoord[0] - va.texcoord[0]
            dv2 = vc.texcoord[1] - va.texcoord[1]
            denominator = du1 * dv2 - du2 * dv1
            if abs(denominator) < 1e-20:
                continue
            reciprocal = 1.0 / denominator
            tangent = tuple((edge1[axis] * dv2 - edge2[axis] * dv1) * reciprocal for axis in range(3))
            for index in (a, b, c):
                _add_in_place(tangents[index], tangent)

    result = []
    for index, vertex in enumerate(mesh.vertices):
        normal = _normalize(vertex.normal)
        tangent = tuple(tangents[index])
        projection = sum(tangent[axis] * normal[axis] for axis in range(3))
        tangent = tuple(tangent[axis] - normal[axis] * projection for axis in range(3))
        if math.sqrt(sum(value * value for value in tangent)) < 1e-20:
            helper = (0.0, 1.0, 0.0) if abs(normal[1]) < 0.9 else (1.0, 0.0, 0.0)
            tangent = _cross(helper, normal)
        tangent = _normalize(tangent)
        binormal = _normalize(_cross(normal, tangent))
        result.append((tangent, binormal))
    return tuple(result)


def _text_bytes(value: str) -> bytes:
    encoded = value.encode("ascii")
    return struct.pack("<I", len(encoded)) + encoded


def geometry_payload_from_obj(
    donor: RGMGeometry,
    mesh: ObjMesh,
    *,
    keep_obj_names: bool = False,
    flip_v: bool = True,
) -> bytes:
    """Build a DATA/DATA v8 payload using a donor's layout/material/skeleton."""

    if not mesh.sections:
        raise RGMParseError("OBJ contains no triangle sections")
    donor_section = max(donor.sections, key=lambda section: len(section.indices))
    if keep_obj_names:
        sections = mesh.sections
    else:
        combined = tuple(index for section in mesh.sections for index in section.indices)
        sections = (ObjSection(donor_section.name, combined),)

    basis = _tangent_basis(mesh)
    fallback = {
        element.semantic: donor._field(0, element)
        for element in donor.layout
        if donor.vertex_count
    }
    vertex_data = bytearray()
    for index, vertex in enumerate(mesh.vertices):
        tangent, binormal = basis[index]
        for element in donor.layout:
            if element.semantic == 0 and element.format_code == 4:
                field = struct.pack("<3f", *vertex.position)
            elif element.semantic == 3 and element.format_code == 2:
                field = _pack_vector(vertex.normal)
            elif element.semantic == 4 and element.format_code == 2:
                field = _pack_vector(binormal)
            elif element.semantic == 5 and element.format_code == 2:
                field = _pack_vector(tangent)
            elif element.semantic == 8 and element.format_code == 2:
                u = max(0.0, min(1.0, vertex.texcoord[0]))
                v_value = 1.0 - vertex.texcoord[1] if flip_v else vertex.texcoord[1]
                v = max(0.0, min(1.0, v_value))
                field = struct.pack("<2H", round(u * 65535), round(v * 65535))
            elif element.semantic == 8 and element.format_code == 3:
                v = 1.0 - vertex.texcoord[1] if flip_v else vertex.texcoord[1]
                field = struct.pack("<2f", vertex.texcoord[0], v)
            else:
                field = fallback.get(element.semantic, bytes(element.size))
            if len(field) != element.size:
                raise RGMParseError(
                    f"cannot encode {element.name}: expected {element.size} bytes, got {len(field)}"
                )
            vertex_data.extend(field)

    pieces = [b"\0", struct.pack("<I", len(sections))]
    for section in sections:
        if len(section.indices) % 3:
            raise RGMParseError(f"OBJ section {section.name!r} is not triangulated")
        if section.indices and max(section.indices) >= len(mesh.vertices):
            raise RGMParseError(f"OBJ section {section.name!r} has an invalid index")
        points = [mesh.vertices[index].position for index in set(section.indices)]
        center = tuple(
            (min(point[axis] for point in points) + max(point[axis] for point in points)) * 0.5
            for axis in range(3)
        )
        name = section.name if keep_obj_names else donor_section.name
        pieces.append(struct.pack("<I", len(section.indices)))
        pieces.append(struct.pack(f"<{len(section.indices)}H", *section.indices))
        pieces.append(struct.pack("<3fB", *center, donor_section.flag))
        pieces.append(_text_bytes(name))

    pieces.append(struct.pack("<I", len(donor.layout)))
    pieces.extend(
        struct.pack("<3I", element.semantic, element.unknown, element.format_code)
        for element in donor.layout
    )
    pieces.append(struct.pack("<2I", len(mesh.vertices), donor.vertex_stride))
    pieces.append(bytes(vertex_data))
    pieces.append(struct.pack("<I", donor.post_vertex_unknown))
    pieces.append(_text_bytes(donor.material))
    pieces.append(struct.pack("<I", len(donor.bones)))
    for bone in donor.bones:
        pieces.append(struct.pack("<12f", *bone.matrix))
        pieces.append(struct.pack("<12f", *bone.inverse_matrix))
        pieces.append(_text_bytes(bone.name))
    pieces.append(donor.tail)
    return b"".join(pieces)


def replace_first_geometry(chunky: ChunkyFile, payload: bytes) -> ChunkyFile:
    """Replace geometry and its topology/bounds companions in the first MRGM."""

    replaced = False
    replacement = parse_geometry_payload(payload)
    replacement_indices = tuple(
        index for section in replacement.sections for index in section.indices
    )
    replacement_index_payload = struct.pack("<I", len(replacement_indices)) + struct.pack(
        f"<{len(replacement_indices)}H", *replacement_indices
    )
    positions = [replacement.position(index) for index in range(replacement.vertex_count)]
    bounds_center = tuple(
        (min(point[axis] for point in positions) + max(point[axis] for point in positions))
        * 0.5
        for axis in range(3)
    )
    bounds_half_extent = tuple(
        (max(point[axis] for point in positions) - min(point[axis] for point in positions))
        * 0.5
        for axis in range(3)
    )
    blend_indices = replacement.element(1)
    target_bone = (
        replacement._field(0, blend_indices)[0]
        if blend_indices is not None and replacement.vertex_count
        else 0
    )

    def update_bimp(chunk: Chunk) -> Chunk:
        assert chunk.children is not None
        idxl_ordinal = 0
        children = []
        for child in chunk.children:
            if child.tag == "DATA/IDXL" and child.version == 1000:
                new_payload = (
                    replacement_index_payload if idxl_ordinal == target_bone else b"\0\0\0\0"
                )
                child = replace(child, payload=new_payload)
                idxl_ordinal += 1
            children.append(child)
        if target_bone >= idxl_ordinal:
            raise RGMParseError(
                f"replacement uses bone {target_bone}, but BIMP has {idxl_ordinal} list(s)"
            )
        return replace(chunk, children=tuple(children))

    def update_companions(chunk: Chunk) -> Chunk:
        if chunk.tag == "DATA/BVOL" and chunk.version == 2 and len(chunk.payload) == 61:
            marker = chunk.payload[0]
            old_half_extent = struct.unpack_from("<3f", chunk.payload, 13)
            if marker == 1 and any(abs(value) > 1e-20 for value in old_half_extent):
                new_payload = (
                    bytes((marker,))
                    + struct.pack("<3f", *bounds_center)
                    + struct.pack("<3f", *bounds_half_extent)
                    + chunk.payload[25:]
                )
                return replace(chunk, payload=new_payload)
        if chunk.tag == "FOLD/BIMP" and chunk.version == 1000 and chunk.children is not None:
            return update_bimp(chunk)
        if chunk.children is not None:
            return replace(
                chunk, children=tuple(update_companions(child) for child in chunk.children)
            )
        return chunk

    def visit(chunks: tuple[Chunk, ...]) -> tuple[Chunk, ...]:
        nonlocal replaced
        output = []
        for chunk in chunks:
            current = chunk
            if not replaced and chunk.tag == "FOLD/MRGM" and chunk.children is not None:
                children = []
                for child in chunk.children:
                    if not replaced and child.tag == "DATA/DATA" and child.version == 8:
                        try:
                            parse_geometry_payload(child.payload)
                        except RGMParseError:
                            pass
                        else:
                            child = replace(child, payload=payload)
                            replaced = True
                    if replaced:
                        child = update_companions(child)
                    children.append(child)
                current = replace(chunk, children=tuple(children))
            elif chunk.children is not None:
                current = replace(chunk, children=visit(chunk.children))
            output.append(current)
        return tuple(output)

    result = replace(chunky, chunks=visit(chunky.chunks))
    if not replaced:
        raise RGMParseError("no replaceable FOLD/MRGM v3 geometry block found")
    return result
