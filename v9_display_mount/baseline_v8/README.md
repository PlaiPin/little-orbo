# Orbo V8 — reinforced internal frame

Open `CAD/Orbo_V8_RigidFrame.f3d` in Fusion. The STEP assembly is alongside it, and `STL` contains the 17 custom printable parts.

The V7 shell, head, diffuser, arm exterior shapes and functional component locations are retained. This revision changes the internal supporting structure and its fasteners. The servo axes, Pi, cooler, battery, U2D2, regulator, motors, wheels and mast remain in their previous positions.

## Structural changes

- The electronics deck becomes a single cage with four columns, front/rear braces and longitudinal side braces. Wider webs carry loads around the concealed screwdriver passages.
- Full-width deck extensions support the servo feet. Each foot has approximately **908 mm² of actual mating contact** with the deck. The feet are 6 mm thick and use longer M3 × 12 screws.
- Broad upright webs and diagonal gussets connect the servo cradles to their feet.
- Four bolted pads connect the servo towers to the neck carrier. These bridge the former unsupported gap while allowing the carrier to be removed for service.
- Two braced lower end frames replace the lower spacer arrangement. The front frame has two diagonal supports fastened to the battery shelf, resisting fore/aft movement as well as lateral movement.
- Four M3 × 80 through bolts clamp the motor plate, lower frames, shelf and upper cage into one stack. Enlarged plate pads support the frame feet. The old eight M3 × 30 spacers are removed.

The load path is servo cradle → gusseted tower → supported foot and neck-carrier ties → braced cage → clamped lower frame → motor plate. The shell is not used as a structural brace.

## Assembly sequence

1. Prepare the printed frame parts. Install the compatible M3 inserts in the deck (four), servo-tower carrier seats (four), and front lower frame (two). Install the servo-ear inserts as in V7. Clear the long Ø3.4 mm bolt passages. The modeled M3 insert occupied volumes are nominal; confirm the selected insert's pilot and installation requirements before printing.
2. Assemble the motors, wheels and casters on the motor plate. Load its four side-entry shell-mounting nuts. Insert the four M3 × 80 frame bolts upward from underneath at X±30, Y−43/49 mm. Support the plate so the bolt heads stay seated.
3. Lower the front and rear lower frames over the four bolts onto the plate's Z19 mm support pads. The front frame's diagonal supports point inward toward the battery shelf. Fit the battery shelf at Z49–52 mm and tighten its two new M3 × 10 screws at X±30, Y−12 mm into the front-frame inserts.
4. Fit and strap the battery in its original position. Prepare the U2D2 and regulator beneath the electronics cage on the bench, including their cable connections. Lower this cage over the four long bolts until its feet seat on the shelf at Z52 mm. Fit four flat washers and four M3 nuts above the deck. Start all four nuts and tighten evenly, checking that the stack seats without rocking or distorting. The Pi and servo modules are still absent.
5. Bring the unchanged lower shell **upward, neck-first**, around the assembled core. Seat its four ledges beneath the motor plate and fasten it with the four V7 countersunk M3 × 10 screws from underneath.
6. Fit each 9 g servo and its horn into its reinforced tower on the bench. With the head/carrier and Pi absent, lower each module through the neck **6 mm inward** from its final position. Lower its foot onto the deck, move it outward 6 mm and tighten its two M3 × 12 foot screws from above.
7. Install the Raspberry Pi and cooler between the towers. Connect cables, keeping them clear of arm motion, the cooler and the concealed driver passages. The sampled Pi insertion path remains clear.
8. Fit the central square mast and locating bushing if not already installed. Lower the neck carrier from above onto the mast and four new tower pads. Start its four M3 × 10 tie screws at X±53, Y±24 mm, then fit the central draw screw. Seat the carrier evenly before tightening the ties. Do not use the screws to force a misaligned printed part into place.
9. Fit the external keyed arm hubs and concealed hub screws, then their smooth removable covers as in V7. Seat the diffuser and head. Tighten the concealed head screws upward using the existing long ball-end driver passages.
10. Check frame seating, cable clearance, wheel rotation and arm movement before powered operation. Native arm joints retain the previous −30° to +10° limits.

For service, remove the head, then undo the four carrier ties and central support connection and lift off the carrier. Remove the Pi for access to the servo-foot screws. Move each tower inward 6 mm before lifting it out. To remove the lower shell, first remove the arm hubs and servo towers, undo the underside shell screws and lower the shell away from the core. The clamped core can then be serviced on the bench; remove its top nuts and lift the upper cage to reach the battery and lower structure.

## Changed parts and hardware

**Print eight new/revised parts:** motor plate, battery shelf, electronics cage, two servo towers, neck carrier, and two lower frames. The other nine custom printed parts, including the exterior torso and arm covers, can be reused from V7.

| Hardware | V8 requirement |
|---|---|
| Frame stack | Four M3 × 80 socket screws, four M3 flat washers (modeled 0.5 mm thick), four M3 hex nuts (modeled 2.4 mm thick) |
| Servo feet | Four M3 × 12 socket screws; replaces the previous M3 × 10 screws |
| Carrier-to-tower ties | Four M3 × 10 socket screws |
| Shelf-to-front-frame ties | Two M3 × 10 socket screws |
| M3 inserts | Ten total: four deck, four carrier seats, two front-frame seats; Ø4.2 mm pilots and nominal 5 mm occupied length modeled |
| Previous spacer stack | Eight M3 × 30 spacers removed |
| Other hardware | Retain V7 shell, head, mast, servo-ear and arm-hub hardware |

All new screws are internal or underneath. No new holes or fasteners are added to the exterior shell.

## Verification and limits

See `VALIDATION.md` and the JSON reports in `review`. Static interference, mating contact, staged insertion, tool access, retained-part geometry/position, exported solids and print meshes are checked. These checks establish geometric assembly feasibility; they do not establish a load rating or measured stiffness. Print material, orientation, insert retention, fastener preload and physical deflection still require a prototype test. Wiring is not modeled as routed flexible geometry. The accepted 23 × 12.2 × 24 mm servo envelope remains provisional, and the V7 arm-cover clips still require a physical fit trial.

Views: [reinforced frame](renders/reinforced_frame.png), [structure and connections](renders/structure_and_connections.png), [servo tower connections](renders/servo_tower_connections.png), [open-neck assembly](renders/open_neck_assembly.png), [unchanged exterior](renders/assembled.png).

The Fusion file contains editable solids, native reinforcement features and assembly joints. The source V3, original Fusion file and previous deliverables remain available. The recorded construction scripts were applied in order: `build_v8.py`, `refine_frame.py`, `finish_v8.py`, `bridge_tool_passages.py`, `finish_fastener_clearance.py`; they expect the V7 source document open and should not be rerun on the finished V8 document indiscriminately.
