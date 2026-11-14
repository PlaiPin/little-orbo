#!/usr/bin/env python3
"""Generate the furnished SDF and its conservative 2 cm occupancy map together.

Primitive geometry is original, offline, and reproducible. Map includes the full
XY collision footprint of furniture, including overhangs, rather than just legs.
"""

from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
COLORS = {
    "wood": ".38 .21 .10 1",
    "oak": ".67 .45 .25 1",
    "wall": ".87 .85 .78 1",
    "teal": ".14 .34 .34 1",
    "cushion": ".23 .47 .44 1",
    "cream": ".86 .77 .59 1",
    "black": ".035 .045 .055 1",
    "screen": ".09 .19 .24 1",
    "green": ".16 .34 .13 1",
    "pot": ".60 .25 .15 1",
    "rug": ".59 .34 .24 1",
    "white": ".94 .91 .83 1",
}


def generate(root=ROOT):
    sdf = ET.Element("sdf", version="1.9")
    w = ET.SubElement(sdf, "world", name="orbo_room")

    def xml(parent, text):
        parent.append(ET.fromstring(text))

    xml(
        w,
        '<physics name="1ms" type="ignored"><max_step_size>0.001</max_step_size><real_time_factor>1</real_time_factor></physics>',
    )
    xml(w, "<gravity>0 0 -9.81</gravity>")
    for name in ["Physics", "UserCommands", "SceneBroadcaster", "Sensors"]:
        filename = {
            "Physics": "physics",
            "UserCommands": "user-commands",
            "SceneBroadcaster": "scene-broadcaster",
            "Sensors": "sensors",
        }[name]
        p = ET.SubElement(
            w,
            "plugin",
            filename=f"gz-sim-{filename}-system",
            name=f"gz::sim::systems::{name}",
        )
        if name == "Sensors":
            ET.SubElement(p, "render_engine").text = "ogre2"
    xml(
        w,
        "<scene><ambient>0.65 0.65 0.65 1</ambient><background>0.20 0.24 0.28 1</background><shadows>true</shadows></scene>",
    )
    xml(
        w,
        '<light name="daylight" type="directional"><pose>0 0 5 0 0 0</pose><direction>-.4 .2 -1</direction><diffuse>.95 .91 .83 1</diffuse><specular>.1 .1 .1 1</specular><cast_shadows>true</cast_shadows></light>',
    )
    obstacles = []

    def box(name, xyz, size, color, solid=True, mapped=True):
        m = ET.SubElement(w, "model", name=name)
        ET.SubElement(m, "static").text = "true"
        ET.SubElement(m, "pose").text = " ".join(map(str, (*xyz, 0, 0, 0)))
        link = ET.SubElement(m, "link", name="body")
        geometry = (
            f"<geometry><box><size>{' '.join(map(str, size))}</size></box></geometry>"
        )
        if solid:
            xml(link, f'<collision name="collision">{geometry}</collision>')
        c = COLORS[color]
        xml(
            link,
            f'<visual name="appearance">{geometry}<material><ambient>{c}</ambient><diffuse>{c}</diffuse></material></visual>',
        )
        if solid and mapped:
            obstacles.append(
                (
                    xyz[0] - size[0] / 2,
                    xyz[1] - size[1] / 2,
                    xyz[0] + size[0] / 2,
                    xyz[1] + size[1] / 2,
                )
            )

    box("floor", (0, 0, -0.05), (4.2, 4.2, 0.1), "oak", mapped=False)
    for i in range(20):
        box(
            f"floor_seam_{i}",
            (-1.995 + i * 0.21, 0, 0.0005),
            (0.006, 4.0, 0.001),
            "wood",
            False,
        )
    box("north_wall", (0, 2.05, 0.85), (4.2, 0.1, 1.7), "wall")
    box("west_wall", (-2.05, 0, 0.85), (0.1, 4, 1.7), "wall")
    box("south_cutaway_wall", (0, -2.05, 0.2), (4.2, 0.1, 0.4), "wall")
    box("east_cutaway_wall", (2.05, 0, 0.2), (0.1, 4, 0.4), "wall")
    box("rug", (0, 0.35, 0.002), (1.8, 1.65, 0.003), "rug", False)
    for x in [-0.78, 0.78]:
        box(f"rug_border_{x}", (x, 0.35, 0.004), (0.025, 1.5, 0.001), "cream", False)
    box("sofa_base", (0, 1.55, 0.19), (1.75, 0.65, 0.38), "teal")
    box("sofa_back", (0, 1.86, 0.48), (1.75, 0.16, 0.55), "teal")
    for x in [-0.79, 0.79]:
        box(f"sofa_arm_{x}", (x, 1.53, 0.4), (0.17, 0.70, 0.43), "teal")
    for i, x in enumerate([-0.48, 0, 0.48]):
        box(f"sofa_seat_{i}", (x, 1.5, 0.405), (0.45, 0.49, 0.09), "cushion")
    for x in [-0.55, 0.55]:
        box(f"pillow_{x}", (x, 1.70, 0.58), (0.25, 0.11, 0.25), "cream")
    box("coffee_table_top", (0, 0.45, 0.30), (0.90, 0.54, 0.10), "wood")
    for x in [-0.36, 0.36]:
        for y in [0.25, 0.65]:
            box(f"table_leg_{x}_{y}", (x, y, 0.125), (0.06, 0.06, 0.25), "black")
    box("book", (0, 0.45, 0.365), (0.24, 0.16, 0.03), "cream", False)
    box("armchair_base", (-1.4, 0.6, 0.20), (0.58, 0.63, 0.4), "cream")
    box("armchair_back", (-1.4, 0.88, 0.49), (0.58, 0.12, 0.45), "cream")
    for x in [-1.65, -1.15]:
        box(f"armchair_arm_{x}", (x, 0.6, 0.38), (0.08, 0.6, 0.32), "cream")
    box("console", (1.65, 0.9, 0.22), (0.48, 1.28, 0.44), "oak")
    box("tv_stand", (1.65, 0.9, 0.50), (0.16, 0.35, 0.12), "black")
    box("television", (1.68, 0.9, 0.83), (0.055, 1.04, 0.60), "black")
    box("tv_screen", (1.648, 0.9, 0.83), (0.009, 0.95, 0.51), "screen", False)
    box("plant_pot", (-1.6, 1.6, 0.16), (0.25, 0.25, 0.32), "pot")
    box("plant_foliage", (-1.6, 1.6, 0.47), (0.38, 0.35, 0.35), "green")
    box("sideboard", (-1.70, -0.7, 0.34), (0.42, 0.8, 0.68), "wood")
    box("picture_frame", (0, 1.988, 1.16), (0.85, 0.025, 0.45), "wood", False)
    box("picture_art", (0, 1.97, 1.16), (0.76, 0.008, 0.36), "teal", False)
    box("picture_sun", (0.20, 1.962, 1.20), (0.16, 0.008, 0.16), "cream", False)
    ET.indent(sdf)
    (root / "worlds/living_room.sdf").write_text(
        ET.tostring(sdf, encoding="unicode") + "\n"
    )
    # Origin -2.2,-2.2, PGM rows run north to south. Mark any intersecting cell.
    n = 220
    resolution = 0.02
    origin = -2.2
    pixels = bytearray()
    for row in range(n):
        y = origin + (n - 1 - row) * resolution
        for col in range(n):
            x = origin + col * resolution
            occupied = any(
                x < xmax
                and x + resolution > xmin
                and y < ymax
                and y + resolution > ymin
                for xmin, ymin, xmax, ymax in obstacles
            )
            pixels.append(0 if occupied else 254)
    (root / "maps").mkdir(exist_ok=True)
    (root / "maps/living_room.pgm").write_bytes(f"P5\n{n} {n}\n255\n".encode() + pixels)
    (root / "maps/living_room.yaml").write_text(
        "image: living_room.pgm\nmode: trinary\nresolution: 0.02\norigin: [-2.2, -2.2, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.25\n"
    )


if __name__ == "__main__":
    generate()
