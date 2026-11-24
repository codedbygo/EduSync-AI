# Demo script (about 5 minutes)

Start the app (`docker run ... ` or the local commands in README), open it, and use the files in `samples/`.

## 1. Video + Slides (2 min)
1. Open **Video + Slides**. Choose `samples/presenter_greenscreen.mp4` and `samples/lecture_slides.pptx`. Click **Start**.
2. Watch the stages: slides converted, speech transcribed (Whisper, local), slides matched.
3. With a Gemini key the render starts by itself. Without one, the timeline editor opens with a draft. Point out that it says the match was not made. Set slides to 0, 11.4, 30.9 and 47.4 seconds (or scrub the source video and use "Use current time"), then **Render video**.
4. Play the result: green is gone, slide on the left changes with the topic, crossfade at each change, audio is the original. Click **Download MP4**.

## 2. Answer Evaluation (3 min, needs the key)
1. Open **Answer Evaluation**, click **Load sample rubric**, upload `samples/answer_correct.png`. Evaluate. Expect near full marks with quoted evidence.
2. Repeat with `answer_partial.png` (wrong arithmetic, missing chlorophyll), `answer_incorrect.png`, and `answer_alt_wording.png` (same ideas in different words: should still score).
3. Upload `answer_unclear.png` (blurred, noisy). Expect low confidence and a "Needs human review" banner instead of a confident grade.
4. Point at the rule: scores can never exceed the rubric, and every awarded mark shows its quote. Download the JSON report.
