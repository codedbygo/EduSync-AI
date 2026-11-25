"""Real files, real FFmpeg and Whisper. No LLM key needed (manual-timeline path)."""
import json, subprocess, time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app import video

S = Path(__file__).resolve().parents[2] / "samples"


def test_clean_timeline_rules():
    raw = [{"slide": 2, "start": 10}, {"slide": 1, "start": 3}, {"slide": 1, "start": 4}, {"slide": 9, "start": 5},
           {"slide": 3, "start": 10.5}, {"slide": 3, "start": 20}, {"slide": 4, "start": 999}]
    tl = video.clean_timeline(raw, 4, 60)
    assert [t["slide"] for t in tl] == [1, 2, 3]       # invalid slide/time dropped, repeats and <2s dwell merged
    assert tl[0]["start"] == 3          # the first slide keeps its time: 0 to 3s is the presenter intro


def test_segment_picks_snap_to_sentence_starts():
    segs = [{"start": 0.0, "end": 3, "text": "a"}, {"start": 3.4, "end": 8, "text": "b"}, {"start": 9.1, "end": 12, "text": "c"}]
    raw = [{"slide": 1, "segment": 0}, {"slide": 2, "segment": 2}, {"slide": 3, "segment": 99}]   # 99 is invalid, dropped
    out = video.segments_to_times(raw, segs)
    assert [(o["slide"], o["start"]) for o in out] == [(1, 0.0), (2, 9.1)]
    assert "#2" in video.build_match_prompt(["x", "y"], segs)


def test_slide_chain_offsets():
    c = video.slide_chain(3, [5.0, 9.0], 20, 0.5)
    assert "offset=5.000" in c and "offset=9.000" in c and c.endswith("[slides]")


def test_pptx_conversion():
    imgs, texts = video.slides_to_images(S / "lecture_slides.pptx", Path("/tmp/edusync_test_slides"))
    assert len(imgs) == 4 and "carbon dioxide" in texts[1]


@pytest.fixture(scope="module")
def client(tmp_path_factory, monkeypatch_module=None):
    import os
    os.environ["DATA_DIR"] = str(tmp_path_factory.mktemp("data"))
    from app import main  # loads .env first; the lines below then force the "no model available" path
    for k in ("GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        os.environ.pop(k, None)
    os.environ["LLM_PROVIDER"] = "gemini"
    for k in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
        os.environ.pop(k, None)
    main.DATA = Path(os.environ["DATA_DIR"])
    with TestClient(main.app) as c:  # runs the lifespan: store.init
        yield c


def wait(c, jid, until):
    for _ in range(300):
        s = c.get(f"/api/video/jobs/{jid}").json()
        if s["stage"] in until or s["error"]:
            return s
        time.sleep(1)
    raise AssertionError("timeout")


def test_end_to_end_manual_timeline_renders_valid_mp4(client):
    r = client.post("/api/video/jobs", files={"video_file": open(S / "presenter_greenscreen.mp4", "rb"), "deck_file": open(S / "lecture_slides.pptx", "rb")})
    s = wait(client, r.json()["id"], {"review"})
    assert s["n_slides"] == 4 and s["weak"] and s["segments"], s            # no key -> weak draft, speech was transcribed
    jid = s["id"]
    tl = [{"slide": 1, "start": 0}, {"slide": 2, "start": 11.36}, {"slide": 3, "start": 30.88}, {"slide": 4, "start": 47.44}]
    assert client.post(f"/api/video/jobs/{jid}/render", json={"timeline": tl}).status_code == 200
    s = wait(client, jid, {"done"})
    assert s["stage"] == "done" and s["result"]["chroma_key"], s
    mp4 = client.get(f"/api/video/jobs/{jid}/result.mp4")
    assert mp4.status_code == 200 and mp4.headers["content-type"] == "video/mp4"
    from app import main
    p = main.DATA / jid / "result.mp4"
    info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(p)]))
    kinds = {x["codec_type"] for x in info["streams"]}
    assert kinds == {"video", "audio"}
    dur = video.probe_duration(p)
    assert abs(dur - video.probe_duration(S / "presenter_greenscreen.mp4")) < 0.2    # audio/video length preserved

    # restart: memory and working files gone; the record, result and slides come back from the store
    import shutil
    main.jobs.clear()
    shutil.rmtree(main.DATA / jid)
    s = client.get(f"/api/video/jobs/{jid}").json()
    assert s["stage"] == "done" and s["timeline"][1]["start"] == 11.36
    assert client.get(f"/api/video/jobs/{jid}/result.mp4").status_code == 200
    assert client.get(f"/api/video/jobs/{jid}/slides/2").status_code == 200
    assert any(i["id"] == jid and i["has_result"] for i in client.get("/api/video/jobs").json()["items"])
    # re-render after the restart: inputs are fetched back from the store
    assert client.post(f"/api/video/jobs/{jid}/render", json={"timeline": tl[:2]}).status_code == 200
    assert wait(client, jid, {"done"})["result"]["slides_used"] == 2


def test_bad_inputs_give_clear_errors(client):
    r = client.post("/api/video/jobs", files={"video_file": ("a.txt", b"x"), "deck_file": ("b.pptx", b"x")})
    assert r.status_code == 415 and "unsupported" in r.json()["detail"]
    r = client.post("/api/eval/jobs", data={"rubric": "{not json"}, files={"pages": ("a.png", b"x", "image/png")})
    assert r.status_code == 422
    r = client.post("/api/eval/jobs", data={"rubric": json.dumps({"questions": []})}, files={"pages": ("a.png", b"x", "image/png")})
    assert r.status_code == 422


def test_intro_keeps_background_then_layout_is_white(tmp_path):
    """Before the first slide: full-frame presenter, original background. After: white canvas, slide on top."""
    from PIL import Image
    imgs, _ = video.slides_to_images(S / "lecture_slides.pptx", tmp_path / "slides")
    out = tmp_path / "out.mp4"
    info = video.render(S / "presenter_greenscreen.mp4", imgs, [{"slide": 1, "start": 8}, {"slide": 2, "start": 20}], out, tmp_path)
    assert info["intro_seconds"] == 8 and info["chroma_key"]

    def px(t, xy):
        f = tmp_path / f"f{t}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(out), "-frames:v", "1", str(f)], check=True)
        return Image.open(f).convert("RGB").getpixel(xy)

    r, g, b = px(3, (340, 20))                  # intro: inside the (portrait) video area, still the green backdrop
    assert g > 100 and g > r + 50 and g > b + 30
    assert min(px(14, (1270, 710))) > 240        # slide phase: the slide fills the frame (white margin)
    assert min(px(14, (1280 - 20 - 360 + 8, 720 - 20 - 270 + 8))) > 240  # presenter window: backdrop replaced by white


def _slide(path, *boxes, bg="white"):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1280, 720), bg)
    d = ImageDraw.Draw(im)
    for x, y in boxes:
        d.text((x, y), "IMPORTANT TEXT " * 3, fill="black")
        d.rectangle([x, y, x + 220, y + 40], outline="black", width=3)
    im.save(path)
    return path


def test_fit_scale_shrinks_only_when_text_would_be_covered(tmp_path):
    from PIL import Image
    assert video.fit_scale(_slide(tmp_path / "a.png")) == 1.0                    # nothing under the window: full size
    assert video.fit_scale(_slide(tmp_path / "b.png", (30, 30))) == 1.0          # text top-left: full size
    sc = video.fit_scale(_slide(tmp_path / "c.png", (1030, 640)))                # text where the window sits
    assert 0.6 <= sc < 1.0
    m = video.content_mask(tmp_path / "c.png")                                   # independent check: scale the content, test the window area
    canvas = Image.new("L", (W2 := 640, H2 := 360), 0)
    canvas.paste(m.resize((int(W2 * sc), int(H2 * sc))), (0, 0))
    window = (video.PIP_X // 2, video.PIP_Y // 2, (video.PIP_X + video.PIP_OUTER_W) // 2, (video.PIP_Y + video.PIP_OUTER_H) // 2)
    assert canvas.crop(window).histogram()[255] <= 3
    full = video.fit_scale(_slide(tmp_path / "d.png", (1030, 640), (30, 640), (1030, 30), (30, 30)))   # busy corners elsewhere
    assert 0.6 <= full < 1.0


def test_text_never_hidden_behind_presenter(tmp_path):
    """Render a slide with a text box exactly where the window would be: the window stays bottom-right, the slide shrinks."""
    from PIL import Image
    slide = _slide(tmp_path / "s.png", (1030, 640))
    out = tmp_path / "o.mp4"
    info = video.render(S / "presenter_greenscreen.mp4", [slide], [{"slide": 1, "start": 0}], out, tmp_path)
    sc = info["min_slide_scale"]
    assert info["presenter_window"] == "bottom-right" and info["shrunk_slides"] == 1 and sc < 1
    f = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "5", "-i", str(out), "-frames:v", "1", str(f)], check=True)
    im = Image.open(f).convert("L")
    x, y = int(1030 * sc), int(660 * sc)                          # left edge of the text box outline, after scaling
    assert min(im.getpixel((x + dx, y + dy)) for dx in range(-3, 4) for dy in range(-3, 4)) < 120   # the text box is visible
    assert x + int(220 * sc) < video.PIP_X                         # and it ends left of the presenter window
