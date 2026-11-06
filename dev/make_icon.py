"""Draw the extension icon (packaging/mcpb/icon.png, 512x512): a thin-film stack of alternating high/low index
layers on a substrate, with an incident light ray and its reflection. Original artwork for MCP for OpenFilters,
deliberately unlike OpenFilters' own icon (the connector is unofficial).

    .venv/Scripts/python dev/make_icon.py
"""

import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 4  # draw at 4x and downsample, for smooth edges
SIZE = 512 * S

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Rounded-square background: deep blue.
d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=110 * S, fill=(18, 38, 74, 255))

# Layer stack: alternating high (amber) and low (teal) index layers over a grey substrate.
left, right = 92 * S, 420 * S
top = 250 * S
heights = [22, 30, 22, 30, 22, 30, 22]
colours = [(242, 176, 66), (64, 186, 196)] * 4
y = top
for h, c in zip(heights, colours):
    d.rectangle([left, y, right, y + h * S], fill=c + (255,))
    y += h * S
d.rectangle([left, y, right, y + 58 * S], fill=(150, 160, 178, 255))  # substrate

# Light ray: incident from the upper left, reflected to the upper right, meeting at the top surface.
hit = (256 * S, top)
width = 16 * S
d.line([(118 * S, 92 * S), hit], fill=(255, 255, 255, 255), width=width)
d.line([hit, (394 * S, 92 * S)], fill=(255, 240, 160, 255), width=width)
r = width // 2
d.ellipse([hit[0] - r, hit[1] - r, hit[0] + r, hit[1] + r], fill=(255, 248, 210, 255))

# Arrow head on the reflected ray.
tip = (394 * S, 92 * S)
d.polygon([tip, (tip[0] - 52 * S, tip[1] + 6 * S), (tip[0] - 18 * S, tip[1] + 48 * S)], fill=(255, 240, 160, 255))

out = os.path.join(ROOT, "packaging", "mcpb", "icon.png")
img.resize((512, 512), Image.LANCZOS).save(out, optimize=True)
print("wrote", os.path.relpath(out, ROOT))
