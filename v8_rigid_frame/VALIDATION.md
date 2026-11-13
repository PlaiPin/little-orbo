# V8 verification

| Check | Result |
|---|---|
| Modified structure against assembly | No structural interference above 0.05 mm³ in checked pairs |
| Servo feet | Approximately 908.29 mm² contact per foot with the supporting deck |
| Carrier-to-tower joints | Four mating pads, approximately 86.15 mm² contact each |
| Lower frame and cage seating | Positive contact at plate, shelf and cage interfaces; measured with 0.01 mm probes |
| Upward shell insertion | 31 sampled offsets clear against changed core parts and frame hardware |
| Servo module insertion | 20 sampled poses clear, with Pi/head/carrier absent; 6 mm inward entry followed by outward placement |
| Pi installation | Eight sampled vertical offsets clear |
| Neck carrier installation | Eight sampled vertical offsets clear after Pi and servo towers |
| Electronics cage installation | Seven sampled vertical offsets clear with battery, lower frames and through bolts present |
| Battery shelf installation | Six sampled vertical offsets clear |
| Tool access | 22 checked paths: frame bolts/nuts, shelf ties, servo feet, carrier ties and concealed head screws |
| Retained geometry and locations | 120 retained bodies match V7 by face count/areas, volume, centre of mass and bounds within the recorded tolerances |
| Native joints | 19 assembly joints, including the frame stack and carrier ties; arm rotation limits retained |
| New frame hardware | 32 simplified hardware bodies checked against structural/equipment solids; no overlaps above 0.02 mm³ after clearance corrections |
| STEP export | 160 parts; each is one solid with valid topology |
| Print meshes | All 17 STL files watertight, in millimetres |
| Original source files | All 62 recorded source hashes unchanged |

The shell-entry check targets the changed core geometry and newly added frame hardware. Unchanged drive/electronics geometry retains the V7 full-core insertion results; the retained-geometry report checks that those bodies did not move. Inserts added afterward occupy existing mounting pockets and do not enlarge the outer insertion envelope.

The geometry/position comparison includes the torso, head, diffuser, arm shapes, visible details, motors, wheels, electronics and mast. The exterior was copied from V7 without remodeling. Structural parts and their replacement hardware are explicitly excluded from that unchanged-body comparison.

Reports: `review/audit.json`, `review/assembly_checks.json`, `review/fastener_checks.json`, `review/retained_geometry_checks.json`, `review/joints.json`, `review/step_validation.json`, and `review/delivery_checks.json`.

Fastener solids are simplified; intentional screw/insert and screw/nut thread engagement is not modeled as helical contact. The native fastener check compares the new hardware with structural/equipment solids, excluding intentional hardware-to-hardware engagement.

These are sampled geometric checks, not a continuous swept-volume proof, stiffness simulation, fatigue analysis or physical load test. Confirm actual printing tolerances and hardware dimensions. Check frame deflection and insert retention on a prototype before treating the structure as load-rated. The previously checked arm motion range and compliant cover mechanism are retained.
