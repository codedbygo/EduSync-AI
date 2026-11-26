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
    for k in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_SECRET_KEY", "BLOB_STORE", "GROQ_API_KEY"):
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
    r = client.post("/api/video/jobs", data={"auto_render": "false"},
                    files={"video_file": open(S / "presenter_greenscreen.mp4", "rb"), "deck_file": open(S / "lecture_slides.pptx", "rb")})
    s = wait(client, r.json()["id"], {"review"})
    assert s["n_slides"] == 4 and s["timeline"] and s["segments"], s        # matched from the speech, waiting for render
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


def test_hosted_transcription_request_and_fallback(monkeypatch):
    import httpx
    seen = {}

    class R:
        status_code = 200
        text = ""
        def json(self):
            return {"segments": [{"start": 0.0, "end": 2.5, "text": " Hello class. "}, {"start": 2.5, "end": 3.0, "text": "  "}]}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        name, f, mime = files["file"]
        seen.update(url=url, auth=headers["Authorization"], fmt=data["response_format"], mime=mime, head=f.read(4))
        return R()
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    out = video.transcribe(S / "presenter_greenscreen.mp4")
    assert out == [{"start": 0.0, "end": 2.5, "text": "Hello class."}]                 # blank segments dropped
    assert seen["url"].endswith("/audio/transcriptions") and seen["auth"] == "Bearer k" and seen["fmt"] == "verbose_json"
    assert seen["mime"] == "audio/ogg" and seen["head"] == b"OggS"                     # real Opus audio was extracted

    def down(*a, **k):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(httpx, "post", down)
    monkeypatch.setattr(video, "_local_transcribe", lambda v: [{"start": 1.0, "end": 2.0, "text": "local"}])
    assert video.transcribe(S / "presenter_greenscreen.mp4")[0]["text"] == "local"   # hosted failure never fails the job


def test_match_accepts_a_bare_list_from_the_model(monkeypatch):
    from app import llm
    segs = [{"start": 0.0, "end": 4.0, "text": "hello"}, {"start": 5.0, "end": 9.0, "text": "a"}, {"start": 10.0, "end": 15.0, "text": "b"}]
    monkeypatch.setattr(llm, "available", lambda: True)
    answer = json.dumps([{"slide": 1, "segment": 1, "confidence": 0.9}, {"slide": 2, "segment": 2, "confidence": 0.9}])
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: llm.parse_json(answer))
    r = video.match(["one", "two"], segs, 30.0)
    assert [t["slide"] for t in r["timeline"]] == [1, 2] and not r["weak"]


def test_gemini_moves_to_next_model_at_once_when_busy(monkeypatch):
    import httpx
    from app import llm
    calls = []

    def post(url, **kw):
        calls.append(url.split("/models/")[1].split(":")[0])
        if "busy" in url:
            return httpx.Response(503, text="high demand")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]})

    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GEMINI_MODEL", "busy-a,busy-b,good")
    monkeypatch.setattr(llm.httpx, "post", post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("must not wait")))
    assert llm.generate_json("x", provider="gemini") == {"ok": 1}
    assert calls == ["busy-a", "busy-b", "good"]


# Real lines from job 12300b4906d3 (851 s video, 21 slides): she announces each slide by number.
CUE_SEGS = [
    {"start": 12.9, "end": 21.16, "text": "and Online Education of Assam Downtown University. Slide 1. Now in our previous module we talked"},
    {"start": 43.6, "end": 53.8, "text": "and also describe the training cycle slide 2 let us now connect this video with the previous module"},
    {"start": 81.84, "end": 91.18, "text": "therefore training requires a systematic process. Slide 3. Now the training process it is a planned"},
    {"start": 203.34, "end": 221.4, "text": "identifying the need it provides the foundation for effective training slide six now once the need is identified the next step is establishing training objectives Now objectives it describes what learners should know or be able to do after training"},
    {"start": 463.85, "end": 464.83, "text": "Slide 12."},
    {"start": 592.59, "end": 594.49, "text": "Slide 15."},
    {"start": 700.0, "end": 705.0, "text": "as we saw in slide 3 the process is a cycle"},  # a look back, not a slide change
    {"start": 764.01, "end": 765.11, "text": "Slide 19."},
    {"start": 818.61, "end": 825.83, "text": "future training again slide 20 so with this thank you all of you for joining me"},
]


def test_spoken_cues_anchor_slides_where_she_says_them():
    a = video.spoken_anchors(CUE_SEGS, [""] * 21)  # image-only deck: the spoken number is taken literally
    spoken = {1: 16.5, 2: 47.5, 3: 86.5, 6: 208.5, 12: 463.9, 15: 592.6, 19: 764.0, 20: 818.6}  # when "slide N" is heard
    assert set(a) == set(spoken)  # the later "as we saw in slide 3" is ignored
    for k, t in spoken.items():
        assert abs(a[k] - t) <= 2.0, (k, a[k], t)
    # the old even split put these 18 to 50 s too early; anchors must not
    for k, even in {12: 445.8, 15: 567.4, 19: 729.5, 20: 770.0}.items():
        assert a[k] - even > 15


def test_slides_without_a_cue_sit_between_their_neighbours_not_across_the_video():
    a = video.spoken_anchors(CUE_SEGS, [""] * 21)
    tl = {t["slide"]: t["start"] for t in video.fill_timeline(a, {}, 21, 851.05)}
    assert tl[1] == a[1] and tl[12] == a[12]
    assert a[3] < tl[4] < tl[5] < a[6]  # 4 and 5 spread between slide 3 and slide 6
    assert a[20] < tl[21] < 851.05
    assert list(tl) == sorted(tl) and list(tl.values()) == sorted(tl.values())


def test_ai_fills_only_uncued_slides_and_only_between_neighbours():
    a = {1: 10.0, 3: 100.0}
    tl = {t["slide"]: (t["start"], t["reason"]) for t in video.fill_timeline(a, {2: (50.0, 0.9, "ai"), 3: (300.0, 0.9, "ai")}, 3, 200.0)}
    assert tl[2] == (50.0, "ai") and tl[3][0] == 100.0  # the cue wins over the AI for slide 3
    tl = {t["slide"]: t["start"] for t in video.fill_timeline(a, {2: (150.0, 0.9, "ai")}, 3, 200.0)}
    assert tl[2] == 55.0  # an AI time past slide 3's cue is refused; interpolated instead


def test_parse_json_handles_fences_wrappers_prose_and_garbage():
    from app import llm
    assert llm.parse_json('```json\n{"timeline": [1]}\n```') == {"timeline": [1]}
    assert llm.parse_json('Here you go:\n[{"slide": 1}]\nHope this helps') == {"items": [{"slide": 1}]}
    assert llm.parse_json('{"a": {"b": [1, 2]}} trailing') == {"a": {"b": [1, 2]}}
    for bad in ("", "no json here", '{"timeline": [1, 2'):
        with pytest.raises(llm.ParseError):
            llm.parse_json(bad)


def test_bad_ai_answer_is_retried_once_with_a_stricter_prompt(monkeypatch):
    from app import llm
    segs = [{"start": 0.0, "end": 4.0, "text": "hello"}, {"start": 5.0, "end": 9.0, "text": "intro"}, {"start": 20.0, "end": 25.0, "text": "photosynthesis"}]
    prompts = []

    def answer(prompt, *a, **k):
        prompts.append(prompt)
        return llm.parse_json("not json at all" if len(prompts) == 1 else '[{"slide":1,"segment":1,"confidence":0.9},{"slide":2,"segment":2,"confidence":0.8}]')

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "generate_json", answer)
    r = video.match(["intro", "photosynthesis"], segs, 40.0)
    assert len(prompts) == 2 and "could not be used" in prompts[1] and "could not be used" not in prompts[0]
    assert [(t["slide"], t["start"]) for t in r["timeline"]] == [(1, 5.0), (2, 20.0)] and not r["weak"]


def test_timeline_rules_are_checked():
    assert video.check_timeline([{"slide": 1, "start": 0}, {"slide": 2, "start": 9}], 2, 10) is None
    assert "increase" in video.check_timeline([{"slide": 1, "start": 5}, {"slide": 2, "start": 3}], 2, 10)
    assert "outside" in video.check_timeline([{"slide": 1, "start": 0}, {"slide": 2, "start": 12}], 2, 10)
    assert "one entry per slide" in video.check_timeline([{"slide": 1, "start": 0}], 2, 10)


def test_nothing_matched_is_a_clearly_marked_guess(monkeypatch):
    from app import llm
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("all models busy")))
    r = video.match(["a", "b"], [{"start": 0.0, "end": 3.0, "text": "hello"}], 30.0)
    assert r["weak"] and "GUESS" in r["note"] and "all models busy" in r["note"] and "Use current time" in r["note"]


def test_cue_numbers_are_matched_to_the_deck_by_content():
    """Her "slide 1" is the deck's 3rd slide: she does not count the name card and the title slide."""
    deck = ["Dr Mumpi Das Assistant Professor University", "Training and Development video lecture semester",
            "Learning Objectives explain meaning concept training process describe steps",
            "Training is important improve knowledge skills enhance performance",
            "Meaning of Training Process systematic sequence activities identify needs objectives"]
    segs = [
        {"start": 0.0, "end": 10.0, "text": "Hello, I am Dr Mumpi Das, assistant professor at the university."},
        {"start": 10.0, "end": 20.0, "text": "Slide 1. In this video we explain the meaning and concept of the training process and describe its steps."},
        {"start": 40.0, "end": 50.0, "text": "Slide 2. Training is important: it improves knowledge and skills and enhances performance."},
        {"start": 80.0, "end": 90.0, "text": "Slide 3. Now the training process is a systematic sequence of activities, from needs to objectives."},
    ]
    a = video.spoken_anchors(segs, deck)
    assert sorted(a) == [3, 4, 5] and a[3] < a[4] < a[5]
    assert abs(a[4] - 39.5) < 1  # the time still comes from when "slide 2" is said


def test_uncued_slides_are_placed_by_their_text_between_the_cues():
    deck = ["Photosynthesis basics", "Chlorophyll absorbs light energy in leaves", "Calvin cycle builds glucose"]
    segs = [{"start": 0.0, "end": 5.0, "text": "Slide 1. Today we look at photosynthesis."},
            {"start": 10.0, "end": 15.0, "text": "Plants need sunlight."},
            {"start": 30.0, "end": 35.0, "text": "Chlorophyll in the leaves absorbs the light energy."},
            {"start": 60.0, "end": 65.0, "text": "Slide 3. The Calvin cycle builds glucose."}]
    a = video.spoken_anchors(segs, deck)
    assert sorted(a) == [1, 3]
    p, how = video.content_proposals(a, deck, segs, 80.0)
    assert how == "shared words" and p[2][0] == 30.0 and p[2][2] == video.TEXT_REASON  # not 10.0: nothing shared
    r = video.match(deck, segs, 80.0)
    assert [(t["slide"], t["start"]) for t in r["timeline"]] == [(1, 0.0), (2, 30.0), (3, 59.5)] and not r["weak"]


def test_ai_is_asked_only_for_slides_the_cues_did_not_place(monkeypatch):
    from app import llm
    segs = [{"start": float(i * 10), "end": float(i * 10 + 5), "text": f"sentence {i}"} for i in range(6)]
    asked = []

    def answer(prompt, *a, **k):
        asked.append(prompt)
        return {"timeline": [{"slide": 2, "segment": 2, "confidence": 0.8}]}

    monkeypatch.setattr(llm, "generate_json", answer)
    p, problem = video.ai_proposals(["a", "b", "c"], segs, 60.0, {1: 0.0, 3: 40.0})
    assert problem is None and p == {2: (20.0, 0.8, "matched by AI")}
    assert "Slide 1 starts at sentence #0" in asked[0] and "Slide 3 starts at sentence #4" in asked[0]
    assert "each of these slides: 2." in asked[0]


def test_the_greeting_stays_a_full_screen_intro(monkeypatch):
    """Even when the AI or the slide text points at the first sentence, slides start after it; a cue still wins."""
    from app import llm
    segs = [{"start": 0.0, "end": 6.0, "text": "Hello learners, I am Dr Das and this is photosynthesis."},
            {"start": 6.0, "end": 12.0, "text": "Photosynthesis turns light into sugar."},
            {"start": 30.0, "end": 35.0, "text": "Slide 2. The Calvin cycle builds glucose."},
            {"start": 50.0, "end": 55.0, "text": "Slide 3. Summary of the lesson."}]
    deck = ["Photosynthesis light sugar Dr Das", "Calvin cycle glucose", "Summary lesson"]
    monkeypatch.setattr(llm, "available", lambda: False)
    tl = video.match(deck, segs, 60.0)["timeline"]
    assert tl[0]["slide"] == 1 and tl[0]["start"] == 6.0  # 0 to 6 s: presenter full screen, then the slides fade in
    cue_first = [{"start": 0.0, "end": 8.0, "text": "Slide 1. Photosynthesis turns light into sugar."}] + segs[2:]
    assert video.match(deck, cue_first, 60.0)["timeline"][0]["start"] == 0.0  # she starts on a slide: no intro


@pytest.mark.real_embeddings
def test_slides_follow_what_is_said_with_no_slide_numbers():
    """No "slide N" anywhere: paraphrased speech is matched to the deck by meaning (real local sentence model)."""
    deck = ["Photosynthesis: plants convert light energy into chemical energy",
            "Inputs and outputs: carbon dioxide and water in, glucose and oxygen out",
            "Average speed equals distance divided by time",
            "Summary and homework"]
    segs = [{"start": 0.0, "end": 5.0, "text": "Hello everyone, welcome back to the class."},
            {"start": 5.0, "end": 15.0, "text": "Green plants capture sunlight and store it as chemical energy."},
            {"start": 15.0, "end": 25.0, "text": "That stored energy is what feeds almost every living thing."},
            {"start": 25.0, "end": 35.0, "text": "What goes in? CO2 from the air and water from the roots."},
            {"start": 35.0, "end": 45.0, "text": "What comes out is sugar, plus the O2 we breathe."},
            {"start": 45.0, "end": 55.0, "text": "Switching topics: a car covers 150 kilometres in two and a half hours."},
            {"start": 55.0, "end": 65.0, "text": "Divide how far it went by how long it took and you get 60 km per hour."},
            {"start": 65.0, "end": 75.0, "text": "To wrap up, please finish the exercises at home before Friday."}]
    r = video.match(deck, segs, 80.0)
    assert [(t["slide"], t["start"]) for t in r["timeline"]] == [(1, 5.0), (2, 25.0), (3, 45.0), (4, 65.0)], r
    assert not r["weak"] and "meaning" in r["note"]


def test_a_slow_ai_never_holds_up_the_job(monkeypatch):
    import time
    from app import llm
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "generate_json", lambda *a, **k: (time.sleep(3), {})[1])
    monkeypatch.setattr(video, "AI_DEADLINE", 0.5)
    deck = ["photosynthesis light energy", "calvin cycle glucose"]
    segs = [{"start": 0.0, "end": 4.0, "text": "hello"}, {"start": 5.0, "end": 9.0, "text": "photosynthesis uses light energy"},
            {"start": 20.0, "end": 25.0, "text": "the calvin cycle makes glucose"}]
    t = time.time()
    r = video.match(deck, segs, 30.0)
    assert time.time() - t < 2.5 and not r["weak"] and "did not answer within" in r["note"]
    assert [(x["slide"], x["start"]) for x in r["timeline"]] == [(1, 5.0), (2, 20.0)]
