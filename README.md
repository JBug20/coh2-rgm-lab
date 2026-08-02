# CoH2 RGM Lab

> **Research status:** no custom model was successfully loaded by CoH2 during
> this investigation. Rebuilt multi-drive tuning archives were omitted from
> the game's menu, tuning-pack art did not override retail art, and expanding
> the retail ArtHigh/ArtLow archives caused an immediate startup crash. Treat
> every archive-writing command as experimental, keep backups, and test only
> offline or in custom games.

An experimental toolkit for investigating custom 3D models in *Company of
Heroes 2*. The first milestone is deliberately conservative: parse the Relic
Chunky container used by `.rgm` files and rebuild it byte-for-byte without
changing the model.

The official CoH2 tools do not include a custom-model exporter. This project is
therefore reverse-engineering work, not a supported Relic workflow. Keep backups
and test only in custom/offline games.

This repository contains source code only. It intentionally excludes extracted
Relic assets, retail archives, Workshop archives, donor models, and third-party
replacement models. Researchers must supply files from their own lawful copies
and respect the licenses of replacement assets.

## Current capabilities

- Validate a Relic Chunky v3 file.
- Print its complete nested chunk tree.
- Export the inspection result as JSON.
- Rebuild parsed files and verify that the result is byte-identical.
- List files inside CoH2 SGA v7 archives without extracting them.
- Extract one selected SGA member, including zlib-compressed members.
- Decode the documented generic-RGM `MRGM/DATA v8` geometry block.
- Export positions, normals, UVs, object groups, and triangles as Wavefront OBJ.
- Import a triangulated Wavefront OBJ and rebuild the first generic-RGM geometry
  block while preserving the donor material, vertex layout, and skeleton.
- Prepare `.blend` scenes with a Blender-side decimation, triangulation, and
  Y-up OBJ export helper.
- Clone a user-owned CoH2 tuning SGA and rebuild its packed CRC-32 and SHA-1
  verification records.
- Build a linked tuning/asset SGA pair so custom `data` files reside in the
  engine's dedicated asset-pack folder.
- Surgically inject a file into a known-working multi-drive tuning SGA while
  preserving its identity, table sizes, entry count, and existing payload bytes.
- Recognize the important model-related chunk families documented so far:
  `MODL`, `MGRP`, `MESH`, `SKEL`, and `MTRL`.

Geometry replacement is experimental. It is intended for offline/custom-game
testing with a donor `.rgm` extracted from a CoH2 installation you own.

## Run without installing

```bash
PYTHONPATH=src python -m coh2_rgm_lab.cli inspect path/to/model.rgm
PYTHONPATH=src python -m coh2_rgm_lab.cli inspect --json path/to/model.rgm
PYTHONPATH=src python -m coh2_rgm_lab.cli roundtrip path/to/model.rgm
PYTHONPATH=src python -m coh2_rgm_lab.cli sga-list ArtArmies.sga --pattern '*.rgm'
PYTHONPATH=src python -m coh2_rgm_lab.cli sga-extract ArtArmies.sga \
  'data/path/from/the/list.rgm' --output sample.rgm
PYTHONPATH=src python -m coh2_rgm_lab.cli sga-inject base-tuning.sga model.rgm \
  --member 'data/art/path/model.rgm' --name 'Model Test' --output ModelTest.sga
PYTHONPATH=src python -m coh2_rgm_lab.cli export-obj generic.rgm \
  --output generic.obj
PYTHONPATH=src python -m coh2_rgm_lab.cli replace-obj donor.rgm prepared.obj \
  --fit-donor --output replacement.rgm
```

To save the losslessly rebuilt copy:

```bash
PYTHONPATH=src python -m coh2_rgm_lab.cli roundtrip \
  path/to/model.rgm --output rebuilt.rgm
```

## Install locally

```bash
python -m pip install -e .
coh2-rgm inspect path/to/model.rgm
```

## First test sample

The ideal first sample is a small vehicle or static prop, together with any
neighboring `.rgo`, `.rga`, and texture/material files bearing the same base
name. Do not download or redistribute somebody else's game assets; extract the
sample from a CoH2 installation you own.

Start with `ArtLowXP2.sga`, `ArtHighXP2.sga`, and `ArtArmies.sga`. Listing is
read-only. Extraction refuses to overwrite an existing output unless `--force`
is passed.

Version 0.2.1 incorporates observations from a retail CoH2 `ArtArmies.sga`:
compressed size precedes unpacked size in its file records, and folder-table
names may be complete backslash-separated paths.

Version 0.3.0 added the first verified geometry milestone. It decodes retail
generic-RGM sections and exports an OBJ without altering the source. The first
validated sample contains 1,668 vertices and 1,022 triangles across two named
objects.

Version 0.4.0 adds the first experimental different-topology writer. It retains
the donor's material, skeleton, layout, and non-geometry chunks, assigns new
vertices using the donor's rigid blend data, and verifies the resulting file by
parsing it again. A successful parse is not yet proof that CoH2 accepts the
replacement; that requires an in-game test.

## Preparing the E-Web Blender file

The downloaded E-Web archive contains `source/turet.blend`. Install Blender on
Garuda, then run the helper from the toolkit directory:

```fish
sudo pacman -S blender

blender --background ~/Documents/COH2mod/e-web/source/turet.blend \
  --python scripts/blender_prepare.py -- \
  --output ~/Documents/COH2mod/e-web/eweb_prepared.obj \
  --triangles 12000
```

The helper exports Y-up geometry, applies modifiers, triangulates the mesh, and
targets roughly 12,000 triangles. Then build the experimental replacement:

```fish
env PYTHONPATH="$HOME/coh2-rgm-lab/src" \
python -m coh2_rgm_lab.cli replace-obj \
  ~/Documents/COH2mod/samples/pintle_dshk38_high.rgm \
  ~/Documents/COH2mod/e-web/eweb_prepared.obj \
  --rotate-y 90 --fit-donor \
  --output ~/Documents/COH2mod/e-web/eweb_donor_test.rgm
```

The first test deliberately keeps the original DShK material and rigid pintle
bone. The E-Web should therefore use the wrong texture and its tripod will move
with the weapon; those are expected limitations for the initial geometry-load
proof.

Version 0.4.1 adds X/Y/Z rotation controls to `replace-obj`. The E-Web sample
uses `--rotate-y 90` so its muzzle follows the DShK donor's +Z firing axis.

Version 0.5.0 added experimental SGA v7 writing through `sga-inject`. It clones
a tuning archive owned by the user, replaces its hidden package identity with a
new visible `.info` entry, adds the replacement asset, and regenerates packed
CRC-32 and SHA-1 verification records. Testing against the retail
`ArdennesAssault.sga` preserved all 420 retained payloads and verified all 422
output entries after adding the E-Web RGM.

Version 0.6.0 incorporates Relic's official example asset pack. It fixes
user-mod `.info` metadata to use a Lua-style dependency table and adds
`sga-pair`, which creates a selectable tuning SGA plus a separate asset SGA and
links them by the asset archive ID.

Version 0.6.1 incorporates a working Workshop tuning pack and fixes two
mount-critical SGA details: Archive.exe's 256-byte pre-data gap and its
breadth-first hierarchical folder table. The writer now preserves all 420
Ardennes tuning paths and payloads while matching the original archive's 282
folder records and pre-data layout.

Version 0.6.2 matches retail `ArtHigh.sga` and `ArtLow.sga` file metadata for
RGM payloads: `data` members use storage type 1 and verification type 0, while
RGD files in the `attrib` drive retain the type 2/SHA-1 policy.

Version 0.7.0 adds `sga-surgical-inject` after rebuilt multi-drive tuning packs
were rejected by the game before appearing in its menu. The command keeps the
known-working archive identity and table extents, repurposes one expendable
single-file `data` leaf when the target path is new, and appends only the new
compressed payload. The original archive must be disabled while testing because
the patched copy intentionally retains the same package ID.

## Building the linked E-Web packs

Generate the test archive locally from the CoH2 installation you own. The base
archive is read only; this writes a separate `EWebTest.sga`:

```fish
cd ~/.local/share/Steam/steamapps/common/Company\ of\ Heroes\ 2

env PYTHONPATH="$HOME/coh2-rgm-lab/src" \
python -m coh2_rgm_lab.cli sga-pair \
  mods/tuning/ArdennesAssault.sga \
  ~/Downloads/eweb_dshk_test.rgm \
  --member 'data/art/armies/common/vehicles/crew/pintle_dshk38/pintle_dshk38.rgm' \
  --name 'E-Web Test' \
  --description 'Experimental custom E-Web model test.' \
  --tuning-output mods/tuning/EWebTest.sga \
  --asset-output mods/assets/EWebAssets.sga
```

Start a custom game, select `E-Web Test` as its tuning pack, and spawn the
Soviet DShK 38 Heavy Machine Gun. Do not overwrite any archive in
`CoH2/Archives`.

## Research basis

The container layout follows Corsix's public `coh2-formats` documentation of
Relic Chunky v3 and the partially documented RGM mesh, skeleton, material, and
marker structures:

- <https://github.com/corsix/coh2-formats/blob/master/relic%20chunky.txt>
- <https://github.com/corsix/coh2-formats/blob/master/rgm.ext>
- <https://github.com/MAK-Relic-Tool/SGA-V7/tree/main/src/relic/sga/v7>
