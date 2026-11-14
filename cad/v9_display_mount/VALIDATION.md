# V9 verification

V9 adds a display mount to a copy of the saved V8 assembly. No original part is intentionally remodeled, moved, deleted or replaced.

| Check | Result |
|---|---|
| Original folder copy | All 239 source files match `baseline_v8/` by SHA-256 |
| Native Fusion preservation | All 160 original bodies retain identical names, face counts, volume, surface area and bounding boxes before/after import |
| Native assembly relationships | All 19 existing as-built joints retained; new assembly fixed in place |
| Final STEP | 201 valid solids: 160 retained + 3 printed mount parts + 12 fasteners + 26 manufacturer display solids |
| STEP retained-body comparison | All 160 retained bodies match a fresh read of the V8 STEP in volume, centroid and bounds within 0.001 mm³ / 0.001 mm |
| Static collisions | None above 0.02 mm³ between new parts and the checked V8 geometry, or between separate new parts |
| Independent native collision check | All 41 new bodies checked in Fusion against all 160 retained V8 bodies; no detected overlaps above 0.02 mm³ |
| Carrier clamping contact | Approximately 396 mm² on each of the upper and lower carrier faces, measured with 0.01 mm contact probes |
| Pedestal installation | Eight sampled vertical offsets clear with head/face stack removed |
| Display installation | Seven sampled vertical offsets clear with retaining bar removed |
| USB plug allowance | 17 × 10 × 12 mm rectangular gauge clear of new mount and hardware |
| Clamp screw tools | Four underside Ø5.2 mm driver paths clear on the removed carrier |
| Printable meshes | Three watertight, consistently wound STLs, each one connected component, millimetres |
| Visual review | Front/rear mount views and open-frame assembly inspected |

Reports: `review/validation.json`, `review/fusion_retained_geometry.json`, `review/step_validation.json`, `review/delivery_checks.json`. The Fusion comparison is made in the actual V8 native archive copy. The final whole-assembly STEP is exported from that Fusion document; the final check compares fresh reads of both saved STEP files, rather than relying on older cached V8 BReps. The construction-stage OCP tests use the copied V8 check geometry. `review/native_assembly_checks.json` records the independent static check in Fusion and contact probes on the unchanged carrier.

The sampled paths are not continuous swept-volume proofs. The USB gauge is an explicit allowance, not a model of every USB cable. Flexible cables, header sockets, a routed harness, vibration, print anisotropy, creep, fastener preload and glass stresses are not simulated. Head installation and tool use must follow the V8 removal/installation sequence; the new carrier clamp is assembled on the bench. Confirm fit and access with a prototype. Manufacturer reference solids may have intended internal component contacts; these are excluded from new-part mutual-interference checks.

## Optical constraint

At the screen center the original mask extends from Y = −61.0 to −54.5 mm: **6.5 mm of solid material**. The display front is at Y = −49.0 mm, with a 5.5 mm air gap behind that planar mask surface. The original smoked cover and decorative eye geometry are also retained. No optical transmission model was supplied, and no successful optical image formation is claimed. An opaque mask blocks the display. Test the intended face material and complete face stack before printing it for animation use.

The 44.22 × 19.80 mm active screen cannot reproduce the original 49.5 mm eye-center spacing and 23.5 mm eye height. Firmware must draw smaller, closer eyes. No optical parts, eye firmware or exterior changes are included in V9.
