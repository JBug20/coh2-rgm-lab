"""Read and rebuild SGA v7 archives used by Company of Heroes 2."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import hashlib
from pathlib import PurePosixPath
import struct
import time
import zlib


MAGIC = b"_ARCHIVE"
VERSION = (7, 0)

_VERSION = struct.Struct("<2H")
_ARCHIVE_HEADER = struct.Struct("<128s3I")
_TOC_HEADER = struct.Struct("<8I")
_FOOTER = struct.Struct("<2I")
_DRIVE = struct.Struct("<64s64s5I")
_FOLDER = struct.Struct("<5I")
_FILE = struct.Struct("<5I2B2I")


class SGAParseError(ValueError):
    """Raised when an archive is not a supported, structurally valid SGA v7."""


@dataclass(frozen=True)
class _Drive:
    alias: str
    name: str
    folder_start: int
    folder_end: int
    file_start: int
    file_end: int
    root_folder: int


@dataclass(frozen=True)
class _Folder:
    name_offset: int
    folder_start: int
    folder_end: int
    file_start: int
    file_end: int


@dataclass(frozen=True)
class SGAEntry:
    """A file entry in an SGA archive."""

    path: str
    data_offset: int
    unpacked_size: int
    packed_size: int
    storage_type: int
    modified_seconds: int
    crc: int
    hash_offset: int
    verification_type: int = 0

    @property
    def compressed(self) -> bool:
        return self.storage_type != 0 or self.packed_size != self.unpacked_size


@dataclass(frozen=True)
class SGAArchive:
    """Parsed SGA v7 metadata plus the original archive bytes."""

    name: str
    entries: tuple[SGAEntry, ...]
    block_size: int
    _raw: bytes

    def find(self, pattern: str = "*") -> tuple[SGAEntry, ...]:
        normalized = pattern.replace("\\", "/").casefold()
        return tuple(
            entry
            for entry in self.entries
            if fnmatch(entry.path.casefold(), normalized)
        )

    def get(self, member: str) -> SGAEntry:
        normalized = member.replace("\\", "/").strip("/").casefold()
        matches = [
            entry for entry in self.entries if entry.path.casefold() == normalized
        ]
        if not matches:
            suffix = "/" + normalized
            matches = [
                entry for entry in self.entries if entry.path.casefold().endswith(suffix)
            ]
        if not matches:
            raise KeyError(f"archive member not found: {member}")
        if len(matches) > 1:
            paths = ", ".join(entry.path for entry in matches[:5])
            raise KeyError(f"archive member is ambiguous: {member} ({paths})")
        return matches[0]

    def read(self, entry: SGAEntry | str) -> bytes:
        if isinstance(entry, str):
            entry = self.get(entry)
        end = entry.data_offset + entry.packed_size
        if entry.data_offset < 0 or end > len(self._raw):
            raise SGAParseError(f"payload outside archive: {entry.path}")
        payload = self._raw[entry.data_offset:end]
        if entry.packed_size != entry.unpacked_size:
            try:
                payload = zlib.decompress(payload)
            except zlib.error as exc:
                raise SGAParseError(
                    f"cannot decompress {entry.path}: {exc}"
                ) from exc
        if len(payload) != entry.unpacked_size:
            raise SGAParseError(
                f"wrong unpacked size for {entry.path}: "
                f"expected {entry.unpacked_size}, got {len(payload)}"
            )
        return payload


def _unpack_at(layout: struct.Struct, raw: bytes, offset: int, label: str) -> tuple:
    if offset < 0 or offset + layout.size > len(raw):
        raise SGAParseError(f"truncated {label} at 0x{offset:x}")
    return layout.unpack_from(raw, offset)


def _ascii(value: bytes, label: str) -> str:
    try:
        return value.rstrip(b"\0").decode("ascii")
    except UnicodeDecodeError as exc:
        raise SGAParseError(f"non-ASCII {label}") from exc


def _archive_name(value: bytes) -> str:
    value = value.rstrip(b"\0")
    if len(value) % 2:
        value += b"\0"
    try:
        return value.decode("utf-16-le").rstrip("\0")
    except UnicodeDecodeError as exc:
        raise SGAParseError("invalid UTF-16 archive name") from exc


def _read_names(raw: bytes, offset: int, count: int) -> dict[int, str]:
    names: dict[int, str] = {}
    cursor = offset
    relative = 0
    for _ in range(count):
        end = raw.find(b"\0", cursor)
        if end < 0:
            raise SGAParseError("unterminated filename table")
        names[relative] = _ascii(raw[cursor:end], "filename")
        size = end - cursor + 1
        cursor = end + 1
        relative += size
    return names


def _name(names: dict[int, str], offset: int) -> str:
    try:
        return names[offset]
    except KeyError as exc:
        raise SGAParseError(f"invalid filename-table offset: {offset}") from exc


def parse_sga_v7(raw: bytes) -> SGAArchive:
    """Parse SGA v7 metadata without modifying or eagerly extracting the archive."""

    minimum = len(MAGIC) + _VERSION.size + _ARCHIVE_HEADER.size
    if len(raw) < minimum:
        raise SGAParseError("file is too small to be an SGA v7 archive")
    if raw[: len(MAGIC)] != MAGIC:
        raise SGAParseError("missing SGA _ARCHIVE signature")

    offset = len(MAGIC)
    version = _unpack_at(_VERSION, raw, offset, "version")
    if version != VERSION:
        raise SGAParseError(
            f"unsupported SGA version {version[0]}.{version[1]}; expected 7.0"
        )
    offset += _VERSION.size

    encoded_name, header_size, data_position, reserved = _unpack_at(
        _ARCHIVE_HEADER, raw, offset, "archive header"
    )
    if reserved != 1:
        raise SGAParseError(f"unexpected reserved header value: {reserved}")
    offset += _ARCHIVE_HEADER.size
    header_position = offset

    toc = _unpack_at(_TOC_HEADER, raw, offset, "table-of-contents header")
    offset += _TOC_HEADER.size
    _unknown, block_size = _unpack_at(_FOOTER, raw, offset, "archive footer")
    (
        drive_position,
        drive_count,
        folder_position,
        folder_count,
        file_position,
        file_count,
        name_position,
        name_count,
    ) = toc

    if header_size and header_position + header_size > len(raw):
        raise SGAParseError("declared archive header extends beyond the file")
    if data_position > len(raw):
        raise SGAParseError("declared archive data position extends beyond the file")

    drives = []
    for index in range(drive_count):
        values = _unpack_at(
            _DRIVE,
            raw,
            header_position + drive_position + index * _DRIVE.size,
            f"drive {index}",
        )
        drives.append(
            _Drive(
                _ascii(values[0], "drive alias"),
                _ascii(values[1], "drive name"),
                *values[2:],
            )
        )

    folders = []
    for index in range(folder_count):
        values = _unpack_at(
            _FOLDER,
            raw,
            header_position + folder_position + index * _FOLDER.size,
            f"folder {index}",
        )
        folders.append(_Folder(*values))

    file_defs = []
    for index in range(file_count):
        values = _unpack_at(
            _FILE,
            raw,
            header_position + file_position + index * _FILE.size,
            f"file {index}",
        )
        file_defs.append(values)

    names = _read_names(raw, header_position + name_position, name_count)
    entries: list[SGAEntry] = []
    emitted: set[int] = set()

    def emit_file(index: int, prefix: PurePosixPath) -> None:
        if not 0 <= index < len(file_defs):
            raise SGAParseError(f"invalid file index: {index}")
        if index in emitted:
            return
        emitted.add(index)
        (
            name_offset,
            relative_data_position,
            packed_size,
            unpacked_size,
            modified_seconds,
            verification_type,
            storage_type,
            crc,
            hash_offset,
        ) = file_defs[index]
        filename = _name(names, name_offset).replace("\\", "/").strip("/")
        path = str(prefix / PurePosixPath(filename))
        entries.append(
            SGAEntry(
                path,
                data_position + relative_data_position,
                unpacked_size,
                packed_size,
                storage_type,
                modified_seconds,
                crc,
                hash_offset,
                verification_type,
            )
        )

    def visit_folder(
        index: int,
        prefix: PurePosixPath,
        drive_prefix: PurePosixPath,
        active: set[int],
    ) -> None:
        if not 0 <= index < len(folders):
            raise SGAParseError(f"invalid folder index: {index}")
        if index in active:
            raise SGAParseError(f"folder cycle at index {index}")
        active.add(index)
        folder = folders[index]
        for file_index in range(folder.file_start, folder.file_end):
            emit_file(file_index, prefix)
        for child_index in range(folder.folder_start, folder.folder_end):
            if not 0 <= child_index < len(folders):
                raise SGAParseError(f"invalid child folder index: {child_index}")
            child = folders[child_index]
            child_name = _name(names, child.name_offset).replace("\\", "/").strip("/")
            # CoH2 commonly stores a folder's complete drive-relative path
            # (for example ``art\\armies\\common``), not just its basename.
            child_prefix = (
                drive_prefix / PurePosixPath(child_name)
                if "/" in child_name
                else prefix / child_name
            )
            visit_folder(child_index, child_prefix, drive_prefix, active)
        active.remove(index)

    for drive in drives:
        if not (
            0 <= drive.folder_start <= drive.folder_end <= len(folders)
            and 0 <= drive.file_start <= drive.file_end <= len(file_defs)
        ):
            raise SGAParseError(f"invalid table range in drive {drive.alias!r}")
        prefix = PurePosixPath(drive.alias) if drive.alias else PurePosixPath()
        visit_folder(drive.root_folder, prefix, prefix, set())

    # Preserve visibility if a damaged-but-readable archive leaves files outside a root.
    for index in range(len(file_defs)):
        if index not in emitted:
            emit_file(index, PurePosixPath("_orphaned"))

    return SGAArchive(
        _archive_name(encoded_name), tuple(entries), block_size, raw
    )


@dataclass(frozen=True)
class _BuildFile:
    path: str
    payload: bytes
    storage_type: int
    verification_type: int
    modified_seconds: int


@dataclass(frozen=True)
class SurgicalInjection:
    """Result metadata for a table-preserving SGA member injection."""

    archive: bytes
    replaced_path: str
    filler_path: str | None


def _normalized_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized or "/" not in normalized:
        raise SGAParseError(f"archive path must include a drive alias: {path!r}")
    if any(part in ("", ".", "..") for part in normalized.split("/")):
        raise SGAParseError(f"unsafe archive path: {path!r}")
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SGAParseError(f"archive path is not ASCII: {path!r}") from exc
    return normalized


def build_sga_v7(
    archive_name: str,
    files: tuple[_BuildFile, ...],
    *,
    block_size: int = 262_144,
) -> bytes:
    """Build a compact SGA v7 with flattened full-path folder records."""

    encoded_archive_name = archive_name.encode("utf-16-le")
    if len(encoded_archive_name) > 128:
        raise SGAParseError("archive name exceeds the 64-character SGA limit")
    if not files:
        raise SGAParseError("cannot build an empty SGA")

    by_path: dict[str, _BuildFile] = {}
    for item in files:
        normalized = _normalized_path(item.path)
        key = normalized.casefold()
        if key in by_path:
            raise SGAParseError(f"duplicate archive path: {normalized}")
        by_path[key] = _BuildFile(
            normalized,
            item.payload,
            item.storage_type,
            item.verification_type,
            item.modified_seconds,
        )

    drive_order = []
    drive_items: dict[str, list[_BuildFile]] = {}
    # Match the ordering produced by Relic's own builders: tuning packs use
    # attrib/info, while the official example asset pack uses info/data.
    preferred = {"attrib": 0, "info": 1, "data": 2}
    for item in by_path.values():
        alias = item.path.split("/", 1)[0]
        drive_items.setdefault(alias, []).append(item)
    drive_order = sorted(
        drive_items,
        key=lambda alias: (preferred.get(alias.casefold(), 100), alias.casefold()),
    )

    folders: list[tuple[int, int, int, int, int]] = []
    file_order: list[_BuildFile] = []
    drives: list[tuple[str, int, int, int, int, int]] = []
    folder_names: list[str] = []

    for alias in drive_order:
        drive_folder_start = len(folders)
        drive_file_start = len(file_order)
        root_index = drive_folder_start

        # Relic's folder table is a breadth-first tree.  Folder names after the
        # first level are complete drive-relative paths, but their child ranges
        # still form a real hierarchy; flattening that tree produces an archive
        # our reader can understand but the game silently refuses to mount.
        root: dict = {"path": "", "children": {}, "files": []}
        for item in drive_items[alias]:
            relative = item.path.split("/", 1)[1]
            if "/" not in relative:
                root["files"].append(item)
                continue
            parts = relative.rsplit("/", 1)[0].split("/")
            node = root
            accumulated: list[str] = []
            for part in parts:
                accumulated.append(part)
                key = part.casefold()
                child = node["children"].get(key)
                if child is None:
                    child = {
                        "path": "/".join(accumulated),
                        "children": {},
                        "files": [],
                    }
                    node["children"][key] = child
                node = child
            node["files"].append(item)

        nodes = [root]
        cursor = 0
        while cursor < len(nodes):
            node = nodes[cursor]
            children = sorted(
                node["children"].values(), key=lambda child: child["path"].casefold()
            )
            node["child_start"] = len(nodes)
            nodes.extend(children)
            node["child_end"] = len(nodes)
            cursor += 1

        def order_files(node: dict) -> None:
            node["file_start"] = len(file_order)
            file_order.extend(
                sorted(node["files"], key=lambda item: item.path.casefold())
            )
            node["file_end"] = len(file_order)
            for child in sorted(
                node["children"].values(), key=lambda item: item["path"].casefold()
            ):
                order_files(child)

        # Archive.exe orders payload records by a depth-first folder walk so
        # every folder's directly contained files occupy one contiguous range.
        order_files(root)

        for node in nodes:
            folders.append(
                (
                    0,
                    drive_folder_start + node["child_start"],
                    drive_folder_start + node["child_end"],
                    node["file_start"],
                    node["file_end"],
                )
            )
            folder_names.append(node["path"].replace("/", "\\"))

        drive_folder_end = len(folders)
        drives.append(
            (
                alias,
                drive_folder_start,
                drive_folder_end,
                drive_file_start,
                len(file_order),
                root_index,
            )
        )

    names = bytearray()
    folder_name_offsets = []
    for name in folder_names:
        folder_name_offsets.append(len(names))
        names.extend(name.encode("ascii") + b"\0")
    file_name_offsets = []
    for item in file_order:
        file_name_offsets.append(len(names))
        names.extend(item.path.rsplit("/", 1)[1].encode("ascii") + b"\0")

    packed_files = []
    hashes = bytearray()
    relative_data_position = 0
    for item in file_order:
        packed = item.payload if item.storage_type == 0 else zlib.compress(item.payload)
        crc = zlib.crc32(packed) & 0xFFFFFFFF
        hash_offset = 0
        if item.verification_type == 4:
            hash_offset = len(hashes)
            hashes.extend(hashlib.sha1(packed).digest())
        packed_files.append(
            (
                relative_data_position,
                packed,
                len(item.payload),
                item.modified_seconds,
                item.verification_type,
                item.storage_type,
                crc,
                hash_offset,
            )
        )
        relative_data_position += len(packed)

    drive_position = _TOC_HEADER.size + _FOOTER.size
    folder_position = drive_position + len(drives) * _DRIVE.size
    file_position = folder_position + len(folders) * _FOLDER.size
    name_position = file_position + len(file_order) * _FILE.size
    hash_position = name_position + len(names)
    header_size = hash_position + len(hashes)
    fixed_prefix_size = len(MAGIC) + _VERSION.size + _ARCHIVE_HEADER.size
    # Every retail and Mod Builder SGA examined leaves this fixed gap between
    # the metadata/header extent and the first packed payload.
    data_padding = 256
    data_position = fixed_prefix_size + header_size + data_padding

    archive_header = _ARCHIVE_HEADER.pack(
        encoded_archive_name.ljust(128, b"\0"), header_size, data_position, 1
    )
    toc = _TOC_HEADER.pack(
        drive_position,
        len(drives),
        folder_position,
        len(folders),
        file_position,
        len(file_order),
        name_position,
        len(folders) + len(file_order),
    )
    footer = _FOOTER.pack(hash_position, block_size)
    drive_table = bytearray()
    for alias, folder_start, folder_end, file_start, file_end, root in drives:
        encoded = alias.encode("ascii")
        if len(encoded) > 63:
            raise SGAParseError(f"drive alias is too long: {alias!r}")
        drive_table.extend(
            _DRIVE.pack(
                encoded.ljust(64, b"\0"),
                encoded.ljust(64, b"\0"),
                folder_start,
                folder_end,
                file_start,
                file_end,
                root,
            )
        )
    folder_table = bytearray()
    for index, (_unused, child_start, child_end, file_start, file_end) in enumerate(folders):
        folder_table.extend(
            _FOLDER.pack(
                folder_name_offsets[index], child_start, child_end, file_start, file_end
            )
        )
    file_table = bytearray()
    for index, packed_info in enumerate(packed_files):
        (
            relative_position,
            packed,
            unpacked_size,
            modified,
            verification,
            storage,
            crc,
            hash_offset,
        ) = packed_info
        file_table.extend(
            _FILE.pack(
                file_name_offsets[index],
                relative_position,
                len(packed),
                unpacked_size,
                modified,
                verification,
                storage,
                crc,
                hash_offset,
            )
        )

    return b"".join(
        [
            MAGIC,
            _VERSION.pack(*VERSION),
            archive_header,
            toc,
            footer,
            bytes(drive_table),
            bytes(folder_table),
            bytes(file_table),
            bytes(names),
            bytes(hashes),
            b"\0" * data_padding,
            *(item[1] for item in packed_files),
        ]
    )


def _mod_info_payload(
    name: str,
    description: str,
    dependencies: tuple[str, ...] = (),
) -> bytes:
    """Build the Lua-style package metadata emitted by the CoH2 Mod Builder."""

    safe_name = name.replace('"', "'")
    safe_description = description.replace('"', "'")
    lines = [
        "hidden = false",
        f'name = "{safe_name}"',
        f'description = "{safe_description}"',
        "dependencies = ",
        "{",
    ]
    for dependency in dependencies:
        if len(dependency) != 32 or any(character not in "0123456789abcdef" for character in dependency):
            raise SGAParseError(f"invalid dependency archive id: {dependency!r}")
        lines.append(f'    "{dependency}",')
    lines.extend(["}", ""])
    return "\r\n".join(lines).encode("ascii", errors="strict")


def inject_tuning_sga(
    base: SGAArchive,
    member: str,
    payload: bytes,
    *,
    name: str,
    description: str,
) -> tuple[bytes, str]:
    """Clone a tuning SGA, replace its info identity, and inject one member."""

    normalized_member = _normalized_path(member)
    identity_seed = name.encode("utf-8") + b"\0" + normalized_member.encode("ascii") + payload
    archive_id = hashlib.sha256(identity_seed).hexdigest()[:32]
    now = int(time.time())
    info_payload = _mod_info_payload(name, description)

    output: dict[str, _BuildFile] = {}
    for entry in base.entries:
        if entry.path.casefold().startswith("info/") and entry.path.casefold().endswith(".info"):
            continue
        output[entry.path.casefold()] = _BuildFile(
            entry.path,
            base.read(entry),
            entry.storage_type,
            entry.verification_type,
            entry.modified_seconds,
        )
    drive_alias = normalized_member.split("/", 1)[0].casefold()
    if drive_alias == "attrib":
        storage_type, verification_type = 2, 4
    else:
        # Retail art archives and working tuning-pack data drives use type 1
        # payload storage without the SHA-1 verification table used by RGD
        # attribute files.
        storage_type, verification_type = 1, 0
    output[normalized_member.casefold()] = _BuildFile(
        normalized_member, payload, storage_type, verification_type, now
    )
    info_path = f"info/{archive_id}.info"
    output[info_path.casefold()] = _BuildFile(info_path, info_payload, 1, 1, now)
    return build_sga_v7(archive_id, tuple(output.values()), block_size=base.block_size), archive_id


def surgical_inject_sga(
    base: SGAArchive,
    member: str,
    payload: bytes,
) -> SurgicalInjection:
    """Inject a member while preserving an accepted archive's overall layout.

    If ``member`` already exists, only its file record is patched and the new
    packed payload is appended.  Otherwise a leaf file in the same drive is
    repurposed.  The filename table keeps its exact original byte size and
    string count; a second low-priority data filename absorbs any freed bytes.

    This deliberately retains the original archive name and ``.info`` payload.
    A patched copy must therefore not be mounted beside its original archive.
    """

    normalized_member = _normalized_path(member)
    raw = bytearray(base._raw)
    header_position = len(MAGIC) + _VERSION.size + _ARCHIVE_HEADER.size
    (
        drive_position,
        drive_count,
        folder_position,
        folder_count,
        file_position,
        file_count,
        name_position,
        name_count,
    ) = _unpack_at(_TOC_HEADER, raw, header_position, "table-of-contents header")
    _encoded_name, _header_size, data_position, _reserved = _unpack_at(
        _ARCHIVE_HEADER,
        raw,
        len(MAGIC) + _VERSION.size,
        "archive header",
    )

    drives: list[_Drive] = []
    for index in range(drive_count):
        values = _unpack_at(
            _DRIVE,
            raw,
            header_position + drive_position + index * _DRIVE.size,
            f"drive {index}",
        )
        drives.append(
            _Drive(
                _ascii(values[0], "drive alias"),
                _ascii(values[1], "drive name"),
                *values[2:],
            )
        )

    folders = [
        _Folder(
            *_unpack_at(
                _FOLDER,
                raw,
                header_position + folder_position + index * _FOLDER.size,
                f"folder {index}",
            )
        )
        for index in range(folder_count)
    ]
    file_defs = [
        _unpack_at(
            _FILE,
            raw,
            header_position + file_position + index * _FILE.size,
            f"file {index}",
        )
        for index in range(file_count)
    ]
    names_start = header_position + name_position
    names = _read_names(raw, names_start, name_count)
    name_items = list(names.items())
    if not name_items:
        raise SGAParseError("archive has no filename table")
    names_end = names_start + sum(len(value.encode("ascii")) + 1 for _, value in name_items)
    old_names_size = names_end - names_start

    file_paths: dict[int, str] = {}
    file_folders: dict[int, int] = {}

    def map_folder(
        folder_index: int,
        prefix: PurePosixPath,
        drive_prefix: PurePosixPath,
        active: set[int],
    ) -> None:
        if folder_index in active:
            raise SGAParseError(f"folder cycle at index {folder_index}")
        active.add(folder_index)
        folder = folders[folder_index]
        for file_index in range(folder.file_start, folder.file_end):
            filename = _name(names, file_defs[file_index][0]).replace("\\", "/").strip("/")
            file_paths[file_index] = str(prefix / PurePosixPath(filename))
            file_folders[file_index] = folder_index
        for child_index in range(folder.folder_start, folder.folder_end):
            child_name = _name(names, folders[child_index].name_offset).replace("\\", "/").strip("/")
            child_prefix = (
                drive_prefix / PurePosixPath(child_name)
                if "/" in child_name
                else prefix / child_name
            )
            map_folder(child_index, child_prefix, drive_prefix, active)
        active.remove(folder_index)

    for drive in drives:
        prefix = PurePosixPath(drive.alias) if drive.alias else PurePosixPath()
        map_folder(drive.root_folder, prefix, prefix, set())

    by_path = {path.casefold(): index for index, path in file_paths.items()}
    target_index = by_path.get(normalized_member.casefold())
    replaced_path = normalized_member
    filler_path: str | None = None
    replacements: dict[int, str] = {}

    if target_index is None:
        target_alias, target_relative = normalized_member.split("/", 1)
        if "/" not in target_relative:
            raise SGAParseError("surgical injection requires a member inside a folder")
        target_folder_name, target_filename = target_relative.rsplit("/", 1)
        target_folder_disk = target_folder_name.replace("/", "\\")

        drive = next(
            (item for item in drives if item.alias.casefold() == target_alias.casefold()),
            None,
        )
        if drive is None:
            raise SGAParseError(
                f"surgical injection cannot add a new drive: {target_alias!r}"
            )

        name_use_count: dict[int, int] = {}
        for folder in folders:
            name_use_count[folder.name_offset] = name_use_count.get(folder.name_offset, 0) + 1
        for values in file_defs:
            name_use_count[values[0]] = name_use_count.get(values[0], 0) + 1

        candidates: list[tuple[int, int, int]] = []
        for folder_index in range(drive.folder_start, drive.folder_end):
            folder = folders[folder_index]
            if folder.folder_start != folder.folder_end or folder.file_end - folder.file_start != 1:
                continue
            file_index = folder.file_start
            if name_use_count.get(folder.name_offset) != 1:
                continue
            if name_use_count.get(file_defs[file_index][0]) != 1:
                continue
            old_folder_name = _name(names, folder.name_offset)
            old_filename = _name(names, file_defs[file_index][0])
            available = len(old_folder_name) + len(old_filename)
            needed = len(target_folder_disk) + len(target_filename)
            if available < needed:
                continue
            path = file_paths.get(file_index, "")
            priority = 1 if "/environment/" in path.casefold() else 0
            candidates.append((priority, available - needed, file_index))
        if not candidates:
            raise SGAParseError(
                "no single-file leaf folder is large enough to repurpose for this path"
            )
        candidates.sort(reverse=True)
        _priority, _spare, target_index = candidates[0]
        target_folder_index = file_folders[target_index]
        old_target_path = file_paths[target_index]
        replacements[folders[target_folder_index].name_offset] = target_folder_disk
        replacements[file_defs[target_index][0]] = target_filename
        replaced_path = old_target_path

        provisional_size = sum(
            len(replacements.get(offset, value).encode("ascii")) + 1
            for offset, value in name_items
        )
        spare = old_names_size - provisional_size
        if spare < 0:
            raise SGAParseError("replacement path does not fit the existing filename table")
        if spare:
            filler_candidates = []
            for file_index, path in file_paths.items():
                if file_index == target_index:
                    continue
                if not path.casefold().startswith(target_alias.casefold() + "/"):
                    continue
                name_offset = file_defs[file_index][0]
                if name_use_count.get(name_offset) != 1 or name_offset in replacements:
                    continue
                priority = 1 if "/environment/" in path.casefold() else 0
                filler_candidates.append((priority, len(path), file_index))
            if not filler_candidates:
                raise SGAParseError("no unique filename can absorb filename-table padding")
            filler_candidates.sort(reverse=True)
            _priority, _length, filler_index = filler_candidates[0]
            filler_offset = file_defs[filler_index][0]
            replacements[filler_offset] = _name(names, filler_offset) + "_" * spare
            filler_path = file_paths[filler_index]

        rebuilt_names = bytearray()
        new_offsets: dict[int, int] = {}
        for old_offset, value in name_items:
            new_offsets[old_offset] = len(rebuilt_names)
            rebuilt_names.extend(replacements.get(old_offset, value).encode("ascii") + b"\0")
        if len(rebuilt_names) != old_names_size:
            raise SGAParseError("internal error: filename table changed size")

        for index, folder in enumerate(folders):
            struct.pack_into(
                "<I",
                raw,
                header_position + folder_position + index * _FOLDER.size,
                new_offsets[folder.name_offset],
            )
        for index, values in enumerate(file_defs):
            struct.pack_into(
                "<I",
                raw,
                header_position + file_position + index * _FILE.size,
                new_offsets[values[0]],
            )
        raw[names_start:names_end] = rebuilt_names

    packed = zlib.compress(payload)
    relative_position = len(raw) - data_position
    crc = zlib.crc32(packed) & 0xFFFFFFFF
    file_record_offset = header_position + file_position + target_index * _FILE.size
    current_name_offset = struct.unpack_from("<I", raw, file_record_offset)[0]
    _FILE.pack_into(
        raw,
        file_record_offset,
        current_name_offset,
        relative_position,
        len(packed),
        len(payload),
        int(time.time()),
        0,
        1,
        crc,
        0,
    )
    raw.extend(packed)

    verified = parse_sga_v7(bytes(raw))
    if verified.read(verified.get(normalized_member)) != payload:
        raise SGAParseError("surgical member failed post-write verification")
    return SurgicalInjection(bytes(raw), replaced_path, filler_path)


def build_linked_sga_pair(
    base: SGAArchive,
    member: str,
    payload: bytes,
    *,
    name: str,
    description: str,
) -> tuple[bytes, str, bytes, str]:
    """Build a tuning SGA and its linked asset SGA for one custom file.

    The tuning archive keeps the base attribute payloads and declares the asset
    archive as a dependency.  The asset archive contains only the injected
    ``data`` member and its own package metadata, matching Relic's example
    asset-pack layout.
    """

    normalized_member = _normalized_path(member)
    if normalized_member.split("/", 1)[0].casefold() != "data":
        raise SGAParseError("asset-pack members must use the data drive")

    now = int(time.time())
    asset_name = f"{name} Assets"
    asset_description = f"Asset dependency for {name}. {description}".strip()
    asset_seed = (
        b"coh2-rgm-lab-asset\0"
        + asset_name.encode("utf-8")
        + b"\0"
        + normalized_member.encode("ascii")
        + b"\0"
        + payload
    )
    asset_id = hashlib.sha256(asset_seed).hexdigest()[:32]
    asset_info_path = f"info/{asset_id}.info"
    asset_files = (
        _BuildFile(normalized_member, payload, 1, 0, now),
        _BuildFile(
            asset_info_path,
            _mod_info_payload(asset_name, asset_description),
            1,
            1,
            now,
        ),
    )
    asset_bytes = build_sga_v7(
        asset_id,
        asset_files,
        block_size=base.block_size,
    )

    tuning_seed = (
        b"coh2-rgm-lab-tuning\0"
        + base.name.encode("utf-8")
        + b"\0"
        + name.encode("utf-8")
        + b"\0"
        + asset_id.encode("ascii")
    )
    tuning_id = hashlib.sha256(tuning_seed).hexdigest()[:32]
    tuning_files: dict[str, _BuildFile] = {}
    for entry in base.entries:
        if entry.path.casefold().startswith("info/") and entry.path.casefold().endswith(".info"):
            continue
        tuning_files[entry.path.casefold()] = _BuildFile(
            entry.path,
            base.read(entry),
            entry.storage_type,
            entry.verification_type,
            entry.modified_seconds,
        )
    tuning_info_path = f"info/{tuning_id}.info"
    tuning_files[tuning_info_path.casefold()] = _BuildFile(
        tuning_info_path,
        _mod_info_payload(name, description, (asset_id,)),
        1,
        1,
        now,
    )
    tuning_bytes = build_sga_v7(
        tuning_id,
        tuple(tuning_files.values()),
        block_size=base.block_size,
    )
    return tuning_bytes, tuning_id, asset_bytes, asset_id
