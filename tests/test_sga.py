from __future__ import annotations

import struct
import unittest
import zlib

from coh2_rgm_lab.sga import (
    _BuildFile,
    SGAParseError,
    build_sga_v7,
    build_linked_sga_pair,
    inject_tuning_sga,
    parse_sga_v7,
    surgical_inject_sga,
)


def _sample_archive(payload: bytes = b"Relic Chunky sample") -> bytes:
    magic = b"_ARCHIVE"
    version = struct.pack("<2H", 7, 0)
    header_position = 8 + 4 + 140
    drive_position = 40
    folder_position = drive_position + 148
    file_position = folder_position + 60
    name_position = file_position + 30
    names = b"\0art\0art\\armies\0sample.rgm\0"
    data_position = header_position + name_position + len(names)
    packed = zlib.compress(payload)

    archive_name = "Synthetic".encode("utf-16-le").ljust(128, b"\0")
    archive_header = struct.pack(
        "<128s3I",
        archive_name,
        data_position - header_position,
        data_position,
        1,
    )
    toc = struct.pack(
        "<8I",
        drive_position,
        1,
        folder_position,
        3,
        file_position,
        1,
        name_position,
        4,
    )
    footer = struct.pack("<2I", 0, 4096)
    drive = struct.pack(
        "<64s64s5I",
        b"data".ljust(64, b"\0"),
        b"Data".ljust(64, b"\0"),
        0,
        3,
        0,
        1,
        0,
    )
    root = struct.pack("<5I", 0, 1, 2, 0, 0)
    art = struct.pack("<5I", 1, 2, 3, 0, 0)
    armies = struct.pack("<5I", 5, 3, 3, 0, 1)
    file_def = struct.pack(
        "<5I2B2I", 16, 0, len(packed), len(payload), 0, 0, 1, 0, 0
    )
    return b"".join(
        [
            magic,
            version,
            archive_header,
            toc,
            footer,
            drive,
            root,
            art,
            armies,
            file_def,
            names,
            packed,
        ]
    )


class SGAParserTests(unittest.TestCase):
    def test_lists_and_extracts_compressed_member(self) -> None:
        raw = _sample_archive()
        archive = parse_sga_v7(raw)
        self.assertEqual(archive.name, "Synthetic")
        self.assertEqual(len(archive.entries), 1)
        self.assertEqual(archive.entries[0].path, "data/art/armies/sample.rgm")
        self.assertEqual(archive.read(archive.entries[0]), b"Relic Chunky sample")

    def test_case_insensitive_glob_and_suffix_lookup(self) -> None:
        archive = parse_sga_v7(_sample_archive())
        self.assertEqual(len(archive.find("*.RGM")), 1)
        self.assertEqual(archive.get("art/armies/sample.rgm"), archive.entries[0])

    def test_rejects_other_versions(self) -> None:
        raw = bytearray(_sample_archive())
        struct.pack_into("<H", raw, 8, 6)
        with self.assertRaises(SGAParseError):
            parse_sga_v7(bytes(raw))

    def test_clone_tuning_archive_and_inject_new_drive(self) -> None:
        base = parse_sga_v7(_sample_archive())
        rebuilt, archive_id = inject_tuning_sga(
            base,
            "data/art/armies/test/replacement.rgm",
            b"replacement geometry",
            name="E-Web Test",
            description="Synthetic test.",
        )
        archive = parse_sga_v7(rebuilt)
        self.assertEqual(archive.name, archive_id)
        self.assertEqual(
            archive.read("data/art/armies/test/replacement.rgm"),
            b"replacement geometry",
        )
        info = archive.read(f"info/{archive_id}.info")
        self.assertIn(b'hidden = false', info)
        self.assertIn(b'name = "E-Web Test"', info)
        self.assertIn(b"dependencies = \r\n{\r\n}", info)
        self.assertEqual(archive.read("data/art/armies/sample.rgm"), b"Relic Chunky sample")

    def test_builds_linked_tuning_and_asset_archives(self) -> None:
        base = parse_sga_v7(_sample_archive())
        member = "data/art/armies/test/replacement.rgm"
        payload = b"replacement geometry"
        tuning_raw, tuning_id, asset_raw, asset_id = build_linked_sga_pair(
            base,
            member,
            payload,
            name="E-Web Test",
            description="Synthetic test.",
        )

        tuning = parse_sga_v7(tuning_raw)
        asset = parse_sga_v7(asset_raw)
        self.assertEqual(tuning.name, tuning_id)
        self.assertEqual(asset.name, asset_id)
        self.assertEqual(asset.read(member), payload)
        asset_member = asset.get(member)
        self.assertEqual(asset_member.storage_type, 1)
        self.assertEqual(asset_member.verification_type, 0)
        self.assertNotIn(member, {entry.path for entry in tuning.entries})
        self.assertIn(
            f'    "{asset_id}",'.encode("ascii"),
            tuning.read(f"info/{tuning_id}.info"),
        )
        self.assertIn(
            b"dependencies = \r\n{\r\n}",
            asset.read(f"info/{asset_id}.info"),
        )

        header_size, data_position, _reserved = struct.unpack_from(
            "<3I", tuning_raw, 12 + 128
        )
        self.assertEqual(data_position, 152 + header_size + 256)

    def test_surgical_inject_preserves_identity_and_adds_member(self) -> None:
        base_raw = build_sga_v7(
            "KnownWorkingIdentity",
            (
                _BuildFile(
                    "data/art/environment/objects/defenses/concrete/ums_mortar_bunker/ums_mortar_bunker_burried.abp",
                    b"sacrificial payload",
                    1,
                    0,
                    1,
                ),
                _BuildFile(
                    "data/art/environment/objects/xp1_test_buildings/base_building_02/base_building_02_replace.abp",
                    b"padding payload",
                    1,
                    0,
                    2,
                ),
                _BuildFile(
                    "data/art/armies/soviet/soldiers/control/control.abp",
                    b"untouched payload",
                    1,
                    0,
                    3,
                ),
                _BuildFile(
                    "info/known.info",
                    b'hidden = false\r\nname = "Known"\r\n',
                    1,
                    1,
                    4,
                ),
            ),
        )
        base = parse_sga_v7(base_raw)
        member = "data/art/armies/common/vehicles/crew/pintle_dshk38/pintle_dshk38.rgm"
        payload = b"replacement RGM geometry"
        result = surgical_inject_sga(base, member, payload)
        archive = parse_sga_v7(result.archive)

        self.assertEqual(archive.name, "KnownWorkingIdentity")
        self.assertEqual(len(archive.entries), len(base.entries))
        self.assertEqual(archive.read(member), payload)
        self.assertEqual(
            archive.read("data/art/armies/soviet/soldiers/control/control.abp"),
            b"untouched payload",
        )
        self.assertGreater(len(result.archive), len(base_raw))
        self.assertIsNotNone(result.filler_path)


if __name__ == "__main__":
    unittest.main()
