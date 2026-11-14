#!/usr/bin/env python3
"""Build original metric keyring geometry and a Gazebo recording camera."""

from pathlib import Path
import math
import xml.etree.ElementTree as E

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ros_ws/src/orbo_simulation/models/keys_scene"
OUT.mkdir(parents=True, exist_ok=True)
vertices = []
faces = []


def torus(cx, cy, cz, r, t):
    start = len(vertices)
    for i in range(48):
        a = 2 * math.pi * i / 48
        for j in range(10):
            b = 2 * math.pi * j / 10
            vertices.append(
                (
                    cx + (r + t * math.cos(b)) * math.cos(a),
                    cy + (r + t * math.cos(b)) * math.sin(a),
                    cz + t * math.sin(b),
                )
            )
    for i in range(48):
        for j in range(10):
            a = start + i * 10 + j
            b = start + ((i + 1) % 48) * 10 + j
            c = start + ((i + 1) % 48) * 10 + (j + 1) % 10
            d = start + i * 10 + (j + 1) % 10
            faces.extend([(a, b, c), (a, c, d)])


def prism(points, z, h):
    start = len(vertices)
    n = len(points)
    vertices.extend((x, y, z) for x, y in points)
    vertices.extend((x, y, z + h) for x, y in points)
    for i in range(1, n - 1):
        faces.extend(
            [
                (start, start + i + 1, start + i),
                (start + n, start + n + i, start + n + i + 1),
            ]
        )
    for i in range(n):
        j = (i + 1) % n
        faces.extend(
            [
                (start + i, start + j, start + n + j),
                (start + i, start + n + j, start + n + i),
            ]
        )


torus(0, 0, 0.006, 0.024, 0.0022)
for angle, offset in [(0.15, 0.015), (-0.7, -0.006)]:
    cx = 0.037
    cy = offset
    torus(cx, cy, 0.006, 0.014, 0.004)
    shape = [
        (0.009, -0.004),
        (0.065, -0.004),
        (0.065, 0.008),
        (0.057, 0.008),
        (0.057, 0.003),
        (0.049, 0.003),
        (0.049, 0.010),
        (0.042, 0.010),
        (0.042, 0.004),
        (0.035, 0.004),
        (0.035, 0.009),
        (0.026, 0.009),
        (0.026, 0.004),
        (0.009, 0.004),
    ]
    points = [
        (
            cx + x * math.cos(angle) - y * math.sin(angle),
            cy + x * math.sin(angle) + y * math.cos(angle),
        )
        for x, y in shape
    ]
    prism(points, 0.004, 0.003)
mesh = OUT / "keys.obj"
mesh.write_text(
    "# Original synthetic keyring geometry; units are metres.\n"
    + "".join(f"v {x:.6f} {y:.6f} {z:.6f}\n" for x, y, z in vertices)
    + "".join("f " + " ".join(str(v + 1) for v in face) + "\n" for face in faces)
)
root = E.Element("sdf", version="1.9")
model = E.SubElement(root, "model", name="keys_scene")
E.SubElement(model, "static").text = "true"
link = E.SubElement(model, "link", name="keys")
E.SubElement(link, "pose").text = ".24 1.03 .002 0 0 0"
v = E.SubElement(link, "visual", name="metal_keys")
g = E.SubElement(v, "geometry")
m = E.SubElement(g, "mesh")
E.SubElement(m, "uri").text = "model://keys_scene/keys.obj"
mat = E.SubElement(v, "material")
for k, val in [
    ("ambient", ".72 .73 .76 1"),
    ("diffuse", ".8 .81 .83 1"),
    ("specular", "1 1 1 1"),
    ("shininess", "90"),
]:
    E.SubElement(mat, k).text = val
v = E.SubElement(link, "visual", name="blue_key_fob")
E.SubElement(v, "pose").text = "-.015 -.038 .006 0 0 -.3"
g = E.SubElement(v, "geometry")
b = E.SubElement(g, "box")
E.SubElement(b, "size").text = ".028 .047 .007"
mat = E.SubElement(v, "material")
for k in ("ambient", "diffuse"):
    E.SubElement(mat, k).text = ".015 .15 .8 1"
cam = E.SubElement(model, "link", name="overview_camera")
E.SubElement(cam, "pose").text = "3 -3.5 3.3 0 .62 2.28"
sensor = E.SubElement(cam, "sensor", name="overview", type="camera")
E.SubElement(sensor, "topic").text = "/orbo_demo/overview"
E.SubElement(sensor, "always_on").text = "true"
E.SubElement(sensor, "update_rate").text = "5"
c = E.SubElement(sensor, "camera")
E.SubElement(c, "horizontal_fov").text = "1.18"
im = E.SubElement(c, "image")
for k, val in [("width", "960"), ("height", "640"), ("format", "R8G8B8")]:
    E.SubElement(im, k).text = val
clip = E.SubElement(c, "clip")
E.SubElement(clip, "near").text = ".05"
E.SubElement(clip, "far").text = "20"
E.indent(root)
(OUT / "model.sdf").write_text(E.tostring(root, encoding="unicode") + "\n")
print(OUT)
