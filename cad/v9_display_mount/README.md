# Orbo V9 — one shared AMOLED display mount

V9 copies the saved V8 assembly and adds a removable mount for **one Waveshare ESP32-S3-AMOLED-1.91**, horizontal and facing forward. Every existing frame, shell, face, hardware and equipment body stays in its V8 position and retains its V8 geometry. Original component names are intentionally retained for traceability. `baseline_v8/` is the complete copied V8 folder, including its native Fusion file, construction history, reports and reusable STLs.

## Open and print

- `CAD/Orbo_V9_DisplayMount.f3d`: native Fusion assembly, preserving the V8 history and assembly relationships, with new components added.
- `CAD/Orbo_V9_DisplayMount.step`: complete assembly for other CAD programs.
- `CAD/V9_Display_Mount_Additions.step`: only the new mount, fasteners and manufacturer's display reference, in robot coordinates.
- `STL/`: three new printable parts. Reuse all existing V8 parts. These STLs use millimetres and assembly coordinates; orient them on the print bed in the slicer.
- `renders/`: the mount on the carrier, rear access, open frame, and unchanged exterior.

## What was added

1. **Display pedestal and carrier saddle.** A two-post support with gussets, an open-backed display cradle, rim-retaining lips, USB-C access and two strain-relief slots. It sits on the existing front neck-carrier rail at Y = −46…−40, Z = 124…128 mm.
2. **Lower clamp jaw.** Four new bolts sandwich the existing carrier rail between the saddle and lower jaw. No hole, insert or adhesive is added to any V8 part.
3. **Removable top retaining bar.** Two screws and captive nuts retain the module by its metal-case perimeter. Do not clamp or press on the glass or active screen.
4. A rigidly positioned copy of Waveshare's 26-solid manufacturer STEP, including headers, connectors, metal case and board components. This purchased component is a reference and must not be printed.

New hardware: **four M3 × 16 socket screws, two M3 × 10 socket screws, six M3 hex nuts** (nominal 5.5 mm across flats, 2.4 mm thick). No existing V8 hardware is replaced. Fasteners are simplified cylinders and hexagons; threads are not modeled. The two retaining nuts slide into rear-access pockets in the cradle columns. The four carrier clamp screws are driven from below with the carrier removed; their nuts sit on the saddle.

The carrier clamp is dimensioned to contact both faces of the existing 4 mm rail. Do not add washers without checking screw length. Tighten evenly and only enough to retain this small module; excessive preload can distort printed parts. Trial-fit the display with the retaining bar loose. Optional thin compliant perimeter pads can take up the nominal 0.3 mm pocket clearance; they are not included as CAD solids. Keep all pads outside the active screen and off PCB components.

## Display location and optics

The supplied drawing specifies a 57.50 × 24.50 mm case and 44.22 × 19.80 mm active area. The active area is **offset along the case**, so the mount centers the active screen at X = 1.25, Z = 176.70 mm, halfway between the original eye graphics. The screen looks toward −Y. The front of the manufacturer's model is at Y = −49.00 mm, leaving 5.50 mm to the rear planar face of the original mask. The active rectangle is X = −20.86…23.36, Z = 166.80…186.60 mm. The USB port exits to the robot's right. Header pins remain behind the screen; the drawing's 14.10 mm total depth is reserved conservatively (the downloaded STEP spans 13.90 mm).

**Optical limitation: the unchanged V8 black mask is a solid panel, 6.5 mm thick at the screen location. An opaque print will completely block the animation.** V8 also has a separate provisional smoked face cover and decorative eye bodies. These remain unchanged and visible in the full assembly. V9 is a mechanical mount revision, not a validated optical face or a projection system. Direct viewing requires a suitably transmissive physical face stack; ordinary opaque printing and heavily diffusing material will not preserve a sharp image. Establish visibility, contrast and image sharpness with a physical sample before committing to the face material. Altering/removing the mask, cover or eye graphics is outside this revision.

One 44.22 × 19.80 mm active area cannot reproduce the original 49.5 mm eye-center spacing or 23.5 mm eye height. Render both eyes smaller and closer together inside the shared display. No mirror, lens, projection optics or firmware is included.

## Assembly and service

1. Power down. Remove the head/face stack using the existing V8 procedure. Remove the neck carrier from its four tower ties and central mast connection for bench assembly.
2. Place the saddle over the front carrier rail. Its small side cheeks locate around the rail with clearance. Place the lower jaw underneath. Fit four M3 × 16 screws upward through the lower jaw and saddle, with four M3 nuts on top. Hold the nuts with a small open-ended wrench and tighten from below. Seat evenly without crushing the rail.
3. Reinstall the carrier and restore every original V8 connection. With the retaining bar and module absent, check access to the original carrier ties and head screws.
4. Insert the two M3 nuts from the rear of the cradle columns. Slide the display down from above with its glass facing forward and USB-C to the right. Seat the metal case against the lower cradle; do not force the module.
5. Fit the top bar and its two M3 × 10 screws. The top bar closes the pocket; it is not intended to preload the glass. Connect the cable from the right and route it rearward/down the posts. Use the two slots for a light cable tie and strain relief. Check the actual USB plug and any soldered harness before reattaching the head.
6. Refit the original head and face components. Check mechanical fit and then perform the optical sample test described above.

For display service, remove the head and top retaining bar and lift the display out. The new cradle leaves the board rear open; no electronics surface is used as a clamping face. With the display removed, the original neck-carrier screws remain accessible. Removing the whole mount restores the V8 configuration.

## Rebuild and evidence

`scripts/inspect_geometry.py` reads the baseline and manufacturer STEP. `scripts/build_v9.py --export` constructs the new geometry and performs the geometric checks before export. They use the existing OCP tooling path recorded at the top of the scripts; adjust that path on another machine. `scripts/fusion_open_v9.py` opens the copied V8 archive in a **new** Fusion document. Run `scripts/fusion_finish_v9.py` there after generating the additions STEP. Do not run any historical V8 construction scripts against a finished V8 or V9 document.

See `VALIDATION.md` and `review/` for measured checks and limits. The new mount requires a print fit trial; geometric checks do not establish strength, vibration life, optical performance, cable bend radius or manufacturing tolerance.

Sources: [Waveshare product documentation](https://docs.waveshare.com/ESP32-S3-AMOLED-1.91), [mechanical drawing and STEP download page](https://docs.waveshare.com/ESP32-S3-AMOLED-1.91/Resources-And-Documents). The downloaded original STEP, ZIP and dimension image are kept in this folder. The supplied manufacturer model is the header-equipped `-M` version; the cradle engages the common metal case, with room reserved for the header option. Confirm the delivered board revision before printing.

