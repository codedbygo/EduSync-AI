"""Generate the sample inputs in this folder. Run from repo root:
    backend/.venv/bin/python samples/make_samples.py
Needs network once: edge-tts (narration) and the Caveat handwriting font (OFL, Google Fonts).
Presenter and handwriting are synthetic stand-ins: no real person or student is involved.
"""
import asyncio
import math
import random
import subprocess
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pptx import Presentation
from pptx.util import Pt

OUT = Path(__file__).parent
FONT = OUT / "Caveat.ttf"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/caveat/Caveat%5Bwght%5D.ttf"

SLIDES = [
    ("Photosynthesis", ["How plants turn light into food", "Why it matters for life on Earth"]),
    ("Inputs and Outputs", ["Inputs: carbon dioxide and water", "Outputs: glucose and oxygen", "6CO2 + 6H2O -> C6H12O6 + 6O2"]),
    ("Where it happens", ["Chloroplasts in leaf cells", "Chlorophyll absorbs light energy", "Light reactions then the Calvin cycle"]),
    ("Summary", ["Light energy becomes chemical energy", "Oxygen is released as a by-product"]),
]
NARRATION = (
    "Welcome everyone. Today we are looking at photosynthesis, which is how plants turn sunlight into food. "
    "It is the reason we have food and oxygen on this planet. "
    "Let us start with what goes in and what comes out. The plant takes in carbon dioxide from the air and water from the soil. "
    "From these it makes glucose, a sugar, and it releases oxygen. The balanced equation is six carbon dioxide plus six water gives glucose plus six oxygen. "
    "Now, where does this happen? Inside the leaf cells there are tiny structures called chloroplasts. "
    "They contain chlorophyll, the green pigment that absorbs light energy. First come the light reactions, and then the Calvin cycle builds the sugar. "
    "To summarise, photosynthesis converts light energy into chemical energy stored in glucose, and oxygen is released as a by-product. Thank you."
)


def make_deck():
    prs = Presentation()
    for title, bullets in SLIDES:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        tf = s.placeholders[1].text_frame
        tf.text = bullets[0]
        for b in bullets[1:]:
            tf.add_paragraph().text = b
        for p in tf.paragraphs:
            p.font.size = Pt(28)
    prs.save(OUT / "lecture_slides.pptx")


def make_video():
    import edge_tts
    mp3 = OUT / "_narration.mp3"
    asyncio.run(edge_tts.Communicate(NARRATION, "en-US-AriaNeural").save(str(mp3)))
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(mp3)]))
    fps, w, h, ss = 25, 640, 720, 2
    ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-",
                           "-i", str(mp3), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-c:a", "aac", "-shortest",
                           str(OUT / "presenter_greenscreen.mp4")], stdin=subprocess.PIPE)
    rnd = random.Random(1)
    for f in range(int(dur * fps)):
        t = f / fps
        img = Image.new("RGB", (w * ss, h * ss))
        d = ImageDraw.Draw(img)
        for y in range(0, h * ss, 8):  # subtle lighting gradient so the key colour is not perfectly flat
            g = int(180 - 20 * y / (h * ss))
            d.rectangle([0, y, w * ss, y + 8], fill=(0, g, 64))
        sway = math.sin(t * 1.3) * 8 * ss
        cx = (w // 2) * ss + sway
        d.ellipse([cx - 230 * ss, 520 * ss, cx + 230 * ss, 900 * ss], fill=(40, 70, 140))        # shoulders
        d.rectangle([cx - 35 * ss, 430 * ss, cx + 35 * ss, 540 * ss], fill=(224, 172, 140))      # neck
        d.ellipse([cx - 95 * ss, 250 * ss, cx + 95 * ss, 470 * ss], fill=(224, 172, 140))        # head
        d.pieslice([cx - 100 * ss, 235 * ss, cx + 100 * ss, 400 * ss], 180, 360, fill=(60, 40, 30))  # hair
        for ex in (-35, 35):
            d.ellipse([cx + (ex - 8) * ss, 345 * ss, cx + (ex + 8) * ss, 361 * ss], fill=(30, 30, 30))
        talking = int(t * 6 + rnd.random()) % 2 == 0
        my = 415 * ss
        d.ellipse([cx - 24 * ss, my, cx + 24 * ss, my + (18 if talking else 6) * ss], fill=(120, 40, 50))
        ff.stdin.write(img.resize((w, h), Image.LANCZOS).tobytes())
    ff.stdin.close()
    ff.wait()
    mp3.unlink()


# ---------- handwriting-style answer sheets ----------
def font(size):
    if not FONT.exists():
        urllib.request.urlretrieve(FONT_URL, FONT)
    f = ImageFont.truetype(str(FONT), size)
    try:
        f.set_variation_by_axes([500])
    except Exception:
        pass
    return f


def page(lines, name, messy=False, diagram=False):
    img = Image.new("RGB", (1240, 1000), (250, 248, 240))
    d = ImageDraw.Draw(img)
    for y in range(90, 1000, 56):
        d.line([(40, y), (1200, y)], fill=(200, 215, 235), width=2)
    rnd = random.Random(name)
    f = font(46 if not messy else 40)
    y = 40
    for ln in lines:
        d.text((60 + rnd.randint(-4, 6), y + rnd.randint(-3, 3)), ln, font=f, fill=(25, 35, 120) if not messy else (110, 120, 170))
        y += 56
    if diagram:
        x0, y0 = 780, 480
        d.ellipse([x0, y0, x0 + 70, y0 + 70], outline=(25, 35, 120), width=4)
        d.text((x0 + 5, y0 - 50), "sun", font=f, fill=(25, 35, 120))
        d.line([(x0 + 60, y0 + 60), (x0 + 200, y0 + 160)], fill=(25, 35, 120), width=4)
        d.polygon([(x0 + 200, y0 + 160), (x0 + 180, y0 + 150), (x0 + 192, y0 + 138)], fill=(25, 35, 120))
        d.ellipse([x0 + 200, y0 + 140, x0 + 360, y0 + 230], outline=(25, 35, 120), width=4)
        d.text((x0 + 215, y0 + 160), "leaf", font=f, fill=(25, 35, 120))
        d.text((x0 + 130, y0 + 240), "chloroplast absorbs light", font=font(34), fill=(25, 35, 120))
    if messy:
        img = img.rotate(2.5, fillcolor=(250, 248, 240)).filter(ImageFilter.GaussianBlur(3.2))
        px = img.load()
        for _ in range(60000):
            x, y2 = rnd.randrange(img.width), rnd.randrange(img.height)
            v = rnd.randint(-60, 60)
            r, g, b = px[x, y2]
            px[x, y2] = (max(0, r + v), max(0, g + v), max(0, b + v))
    img.save(OUT / f"{name}.png")


def make_answers():
    page(["Q1. Photosynthesis is how plants make food using light.",
          "Inputs are carbon dioxide and water. Chlorophyll in the",
          "chloroplasts absorbs light energy. It produces glucose",
          "and oxygen.  6CO2 + 6H2O -> C6H12O6 + 6O2",
          "", "Q2. speed = distance / time = 150 / 2.5 = 60 km/h"], "answer_correct", diagram=True)
    page(["Q1. Plants take in carbon dioxide and water and make",
          "glucose using sunlight. Oxygen is given out too.",
          "", "Q2. speed = distance / time",
          "= 150 / 2.5 = 65 km/h"], "answer_partial")
    page(["Q1. Photosynthesis is when plants breathe in oxygen",
          "and release carbon dioxide at night. They eat soil",
          "to get food.", "", "Q2. speed = 150 x 2.5 = 375 km/h"], "answer_incorrect")
    page(["Q1. Green plants use sunlight captured by the pigment",
          "chlorophyll (inside chloroplasts) to turn CO2 and H2O",
          "into sugar (glucose) while giving off O2.",
          "Word eq: carbon dioxide + water -> glucose + oxygen",
          "", "Q2. v = s/t. v = 150 km / 2.5 h = 60 kilometres per hour"], "answer_alt_wording")
    page(["Q1. Photosynthesis ... plants ... sunlight ... glucose",
          "co2 + h2o ... oxygen ...", "", "Q2. 150 / 2.5 = 60"], "answer_unclear", messy=True)


if __name__ == "__main__":
    make_deck()
    make_answers()
    make_video()
    print("samples written to", OUT)
