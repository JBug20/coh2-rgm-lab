"""Command-line entry point for CoH2 RGM Lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .chunky import (
    EXPECTED_SIGNATURE2,
    ChunkParseError,
    first_difference,
    parse_chunky,
    tags,
)
from .sga import (
    SGAParseError,
    build_linked_sga_pair,
    inject_tuning_sga,
    parse_sga_v7,
    surgical_inject_sga,
)
from .rgm import (
    RGMParseError,
    export_obj,
    geometries,
    geometry_payload_from_obj,
    parse_obj,
    replace_first_geometry,
    transform_obj_to_donor,
)


MODEL_TAGS = {
    "FOLD/MODL": "model root",
    "FOLD/MGRP": "mesh group",
    "FOLD/MESH": "mesh",
    "FOLD/SKEL": "skeleton",
    "FOLD/MTRL": "material",
}


def _load(path: Path) -> tuple[bytes, object]:
    raw = path.read_bytes()
    return raw, parse_chunky(raw)


def _cmd_inspect(args: argparse.Namespace) -> int:
    path = args.file.resolve()
    raw, chunky = _load(path)
    if args.json:
        report = chunky.as_dict()
        report["path"] = str(path)
        report["recognized_model_sections"] = {
            tag: meaning for tag, meaning in MODEL_TAGS.items() if tag in tags(chunky.chunks)
        }
        print(json.dumps(report, indent=2))
        return 0

    print(f"File: {path}")
    print(f"Size: {len(raw)} bytes")
    print(
        f"Relic Chunky: version {chunky.version}, data offset "
        f"0x{chunky.data_offset:x}, {chunky.chunk_count} chunk(s)"
    )
    if chunky.signature2 != EXPECTED_SIGNATURE2:
        print(
            f"Warning: signature2 is {chunky.signature2}; "
            f"documented value is {EXPECTED_SIGNATURE2}"
        )
    present = tags(chunky.chunks)
    recognized = [
        f"{tag} ({meaning})" for tag, meaning in MODEL_TAGS.items() if tag in present
    ]
    if recognized:
        print("Model sections: " + ", ".join(recognized))
    else:
        print("Model sections: none of the currently recognized RGM tags found")
    print()
    for depth, chunk in chunky.walk():
        indent = "  " * depth
        name = f" name={chunk.name_text!r}" if chunk.name else ""
        print(
            f"{indent}0x{chunk.offset:08x} {chunk.tag} v{chunk.version} "
            f"data={chunk.data_size}{name}"
        )
    return 0


def _cmd_roundtrip(args: argparse.Namespace) -> int:
    path = args.file.resolve()
    raw, chunky = _load(path)
    rebuilt = chunky.to_bytes()
    difference = first_difference(raw, rebuilt)
    if difference is not None:
        print(
            f"FAIL: rebuilt data differs at byte 0x{difference:x} "
            f"(original={len(raw)}, rebuilt={len(rebuilt)})",
            file=sys.stderr,
        )
        return 2
    if args.output is not None:
        output = args.output.resolve()
        output.write_bytes(rebuilt)
        print(f"PASS: byte-identical rebuild written to {output}")
    else:
        print(f"PASS: {path} rebuilt byte-for-byte ({len(raw)} bytes)")
    return 0


def _cmd_sga_list(args: argparse.Namespace) -> int:
    path = args.archive.resolve()
    archive = parse_sga_v7(path.read_bytes())
    entries = archive.find(args.pattern)
    if args.json:
        print(
            json.dumps(
                {
                    "path": str(path),
                    "name": archive.name,
                    "version": "7.0",
                    "entry_count": len(archive.entries),
                    "matched_count": len(entries),
                    "entries": [
                        {
                            "path": entry.path,
                            "unpacked_size": entry.unpacked_size,
                            "packed_size": entry.packed_size,
                            "compressed": entry.compressed,
                        }
                        for entry in entries
                    ],
                },
                indent=2,
            )
        )
        return 0
    print(
        f"SGA v7: {archive.name or path.name} — "
        f"{len(entries)}/{len(archive.entries)} entries matched"
    )
    for entry in entries:
        marker = "zlib" if entry.compressed else "raw "
        print(
            f"{entry.unpacked_size:10d} {entry.packed_size:10d} "
            f"{marker} {entry.path}"
        )
    return 0


def _cmd_sga_extract(args: argparse.Namespace) -> int:
    path = args.archive.resolve()
    archive = parse_sga_v7(path.read_bytes())
    entry = archive.get(args.member)
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise OSError(f"refusing to overwrite existing file: {output} (use --force)")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = archive.read(entry)
    output.write_bytes(payload)
    print(f"Extracted {entry.path} ({len(payload)} bytes) to {output}")
    return 0


def _cmd_sga_inject(args: argparse.Namespace) -> int:
    base_path = args.base.resolve()
    source_path = args.file.resolve()
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise OSError(f"refusing to overwrite existing file: {output} (use --force)")
    base = parse_sga_v7(base_path.read_bytes())
    payload = source_path.read_bytes()
    rebuilt, archive_id = inject_tuning_sga(
        base,
        args.member,
        payload,
        name=args.name,
        description=args.description,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rebuilt)

    verified = parse_sga_v7(output.read_bytes())
    if verified.read(verified.get(args.member)) != payload:
        raise SGAParseError("injected member failed post-write verification")
    info = verified.get(f"info/{archive_id}.info")
    print(
        f"Wrote tuning SGA {archive_id} with {len(verified.entries)} entries "
        f"to {output}"
    )
    print(f"Injected {args.member} ({len(payload)} bytes)")
    print(verified.read(info).decode("ascii").rstrip())
    return 0


def _cmd_sga_pair(args: argparse.Namespace) -> int:
    base_path = args.base.resolve()
    source_path = args.file.resolve()
    tuning_output = args.tuning_output.resolve()
    asset_output = args.asset_output.resolve()
    if tuning_output == asset_output:
        raise OSError("tuning and asset outputs must be different files")
    for output in (tuning_output, asset_output):
        if output.exists() and not args.force:
            raise OSError(f"refusing to overwrite existing file: {output} (use --force)")

    base = parse_sga_v7(base_path.read_bytes())
    payload = source_path.read_bytes()
    tuning_bytes, tuning_id, asset_bytes, asset_id = build_linked_sga_pair(
        base,
        args.member,
        payload,
        name=args.name,
        description=args.description,
    )
    tuning_output.parent.mkdir(parents=True, exist_ok=True)
    asset_output.parent.mkdir(parents=True, exist_ok=True)
    tuning_output.write_bytes(tuning_bytes)
    asset_output.write_bytes(asset_bytes)

    tuning = parse_sga_v7(tuning_output.read_bytes())
    asset = parse_sga_v7(asset_output.read_bytes())
    if asset.read(asset.get(args.member)) != payload:
        raise SGAParseError("asset member failed post-write verification")
    tuning_info = tuning.read(f"info/{tuning_id}.info")
    asset_info = asset.read(f"info/{asset_id}.info")
    if asset_id.encode("ascii") not in tuning_info:
        raise SGAParseError("tuning dependency failed post-write verification")

    print(f"Wrote tuning SGA {tuning_id} to {tuning_output}")
    print(f"Wrote asset SGA  {asset_id} to {asset_output}")
    print(f"Injected {args.member} ({len(payload)} bytes) into the asset pack")
    print("Tuning metadata:")
    print(tuning_info.decode("ascii").rstrip())
    print("Asset metadata:")
    print(asset_info.decode("ascii").rstrip())
    return 0


def _cmd_sga_surgical_inject(args: argparse.Namespace) -> int:
    base_path = args.base.resolve()
    source_path = args.file.resolve()
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise OSError(f"refusing to overwrite existing file: {output} (use --force)")

    base = parse_sga_v7(base_path.read_bytes())
    payload = source_path.read_bytes()
    result = surgical_inject_sga(base, args.member, payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result.archive)

    verified = parse_sga_v7(output.read_bytes())
    if verified.name != base.name:
        raise SGAParseError("surgical output did not preserve the archive identity")
    if verified.read(verified.get(args.member)) != payload:
        raise SGAParseError("surgical member failed post-write verification")

    print(f"Wrote identity-preserving SGA {verified.name} to {output}")
    print(f"Injected {args.member} ({len(payload)} bytes)")
    if result.replaced_path.casefold() != args.member.replace("\\", "/").strip("/").casefold():
        print(f"Repurposed leaf member: {result.replaced_path}")
    if result.filler_path is not None:
        print(f"Renamed padding member: {result.filler_path}")
    print("Important: disable the original archive before mounting this copy; both have the same ID.")
    return 0


def _cmd_export_obj(args: argparse.Namespace) -> int:
    source = args.file.resolve()
    _raw, chunky = _load(source)
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise OSError(f"refusing to overwrite existing file: {output} (use --force)")
    output.parent.mkdir(parents=True, exist_ok=True)
    items = export_obj(chunky, output, flip_v=not args.keep_v)
    sections = sum(len(item.sections) for item in items)
    vertices = sum(item.vertex_count for item in items)
    triangles = sum(
        section.triangle_count for item in items for section in item.sections
    )
    print(
        f"Exported {sections} object(s), {vertices} vertices, "
        f"{triangles} triangles to {output}"
    )
    for item in items:
        names = ", ".join(section.name for section in item.sections)
        print(f"Material {item.material}: {names}")
    return 0


def _cmd_replace_obj(args: argparse.Namespace) -> int:
    donor_path = args.donor.resolve()
    obj_path = args.obj.resolve()
    output = args.output.resolve()
    if output.exists() and not args.force:
        raise OSError(f"refusing to overwrite existing file: {output} (use --force)")

    _raw, chunky = _load(donor_path)
    donor = geometries(chunky)[0]
    mesh = parse_obj(obj_path.read_text(encoding="utf-8", errors="strict"))
    mesh = transform_obj_to_donor(
        mesh,
        donor,
        fit=args.fit_donor,
        scale=args.scale,
        translate=(args.x, args.y, args.z),
        rotate=(args.rotate_x, args.rotate_y, args.rotate_z),
    )
    payload = geometry_payload_from_obj(
        donor,
        mesh,
        keep_obj_names=args.keep_obj_names,
        flip_v=not args.keep_v,
    )
    rebuilt = replace_first_geometry(chunky, payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rebuilt.to_bytes())

    # Parse the written bytes again so success means the new container and
    # geometry both survive a clean read.
    verified = geometries(parse_chunky(output.read_bytes()))[0]
    print(
        f"Wrote {verified.vertex_count} vertices and "
        f"{sum(section.triangle_count for section in verified.sections)} triangles "
        f"to {output}"
    )
    print(f"Preserved donor material {verified.material!r} and {len(verified.bones)} bone(s)")
    if args.fit_donor:
        print("Applied uniform fit and centering to the donor geometry bounds")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coh2-rgm",
        description="Inspect and losslessly validate CoH2 Relic Chunky/RGM files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="print the chunk tree")
    inspect_parser.add_argument("file", type=Path)
    inspect_parser.add_argument("--json", action="store_true", help="emit JSON")
    inspect_parser.set_defaults(func=_cmd_inspect)

    roundtrip_parser = subparsers.add_parser(
        "roundtrip", help="parse, rebuild, and require byte-identical output"
    )
    roundtrip_parser.add_argument("file", type=Path)
    roundtrip_parser.add_argument("--output", type=Path)
    roundtrip_parser.set_defaults(func=_cmd_roundtrip)

    sga_list_parser = subparsers.add_parser(
        "sga-list", help="list files in a CoH2 SGA v7 archive"
    )
    sga_list_parser.add_argument("archive", type=Path)
    sga_list_parser.add_argument(
        "--pattern", default="*", help="case-insensitive glob, e.g. '*.rgm'"
    )
    sga_list_parser.add_argument("--json", action="store_true", help="emit JSON")
    sga_list_parser.set_defaults(func=_cmd_sga_list)

    sga_extract_parser = subparsers.add_parser(
        "sga-extract", help="extract one file from a CoH2 SGA v7 archive"
    )
    sga_extract_parser.add_argument("archive", type=Path)
    sga_extract_parser.add_argument("member", help="member path from sga-list")
    sga_extract_parser.add_argument("--output", required=True, type=Path)
    sga_extract_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    sga_extract_parser.set_defaults(func=_cmd_sga_extract)

    sga_inject_parser = subparsers.add_parser(
        "sga-inject",
        help="clone a tuning SGA, replace its identity, and inject one file",
    )
    sga_inject_parser.add_argument("base", type=Path, help="existing user-owned tuning SGA")
    sga_inject_parser.add_argument("file", type=Path, help="local file to inject")
    sga_inject_parser.add_argument(
        "--member", required=True, help="full SGA path including drive alias"
    )
    sga_inject_parser.add_argument("--name", default="CoH2 RGM Test")
    sga_inject_parser.add_argument(
        "--description", default="Experimental custom-model test tuning pack."
    )
    sga_inject_parser.add_argument("--output", required=True, type=Path)
    sga_inject_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    sga_inject_parser.set_defaults(func=_cmd_sga_inject)

    sga_pair_parser = subparsers.add_parser(
        "sga-pair",
        help="build a tuning SGA linked to a separate custom-asset SGA",
    )
    sga_pair_parser.add_argument("base", type=Path, help="existing user-owned tuning SGA")
    sga_pair_parser.add_argument("file", type=Path, help="local asset file to inject")
    sga_pair_parser.add_argument(
        "--member", required=True, help="full data-drive path for the asset"
    )
    sga_pair_parser.add_argument("--name", default="CoH2 RGM Test")
    sga_pair_parser.add_argument(
        "--description", default="Experimental custom-model test tuning pack."
    )
    sga_pair_parser.add_argument("--tuning-output", required=True, type=Path)
    sga_pair_parser.add_argument("--asset-output", required=True, type=Path)
    sga_pair_parser.add_argument(
        "--force", action="store_true", help="overwrite existing output files"
    )
    sga_pair_parser.set_defaults(func=_cmd_sga_pair)

    sga_surgical_parser = subparsers.add_parser(
        "sga-surgical-inject",
        help="preserve a working SGA layout and repurpose one data entry",
    )
    sga_surgical_parser.add_argument(
        "base", type=Path, help="known-working tuning SGA to preserve"
    )
    sga_surgical_parser.add_argument("file", type=Path, help="local file to inject")
    sga_surgical_parser.add_argument(
        "--member", required=True, help="full SGA path including drive alias"
    )
    sga_surgical_parser.add_argument("--output", required=True, type=Path)
    sga_surgical_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    sga_surgical_parser.set_defaults(func=_cmd_sga_surgical_inject)

    export_obj_parser = subparsers.add_parser(
        "export-obj", help="export generic-RGM geometry as Wavefront OBJ"
    )
    export_obj_parser.add_argument("file", type=Path)
    export_obj_parser.add_argument("--output", required=True, type=Path)
    export_obj_parser.add_argument(
        "--keep-v", action="store_true", help="do not flip the texture V coordinate"
    )
    export_obj_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    export_obj_parser.set_defaults(func=_cmd_export_obj)

    replace_obj_parser = subparsers.add_parser(
        "replace-obj",
        help="replace a donor generic-RGM geometry block with a triangulated OBJ",
    )
    replace_obj_parser.add_argument("donor", type=Path, help="vanilla donor .rgm")
    replace_obj_parser.add_argument("obj", type=Path, help="replacement Wavefront .obj")
    replace_obj_parser.add_argument("--output", required=True, type=Path)
    replace_obj_parser.add_argument(
        "--fit-donor",
        action="store_true",
        help="uniformly scale and center the OBJ within the donor model bounds",
    )
    replace_obj_parser.add_argument("--scale", type=float, default=1.0)
    replace_obj_parser.add_argument("--x", type=float, default=0.0)
    replace_obj_parser.add_argument("--y", type=float, default=0.0)
    replace_obj_parser.add_argument("--z", type=float, default=0.0)
    replace_obj_parser.add_argument("--rotate-x", type=float, default=0.0)
    replace_obj_parser.add_argument("--rotate-y", type=float, default=0.0)
    replace_obj_parser.add_argument("--rotate-z", type=float, default=0.0)
    replace_obj_parser.add_argument(
        "--keep-obj-names",
        action="store_true",
        help="keep OBJ object/group names instead of using the donor's main section name",
    )
    replace_obj_parser.add_argument(
        "--keep-v", action="store_true", help="do not flip the texture V coordinate"
    )
    replace_obj_parser.add_argument(
        "--force", action="store_true", help="overwrite an existing output file"
    )
    replace_obj_parser.set_defaults(func=_cmd_replace_obj)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, KeyError, ChunkParseError, SGAParseError, RGMParseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
