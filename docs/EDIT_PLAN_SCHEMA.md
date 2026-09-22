# EditPlan — schema, rules, presets and render recipe

The `EditPlan` is the ONLY contract between the AI planner and the renderer. The LLM produces JSON that must validate
against this schema. The renderer trusts only validated plans.

Time bases: `clip.src_in/src_out` = seconds in the SOURCE video. `overlay.start/end` = seconds in the OUTPUT timeline.

## 1. Pydantic models (copy to `app/schemas/edit_plan.py`)
```python
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Aspect = Literal["9:16", "16:9", "1:1", "original"]
StylePreset = Literal["dynamic_reels", "clean_talk", "ad_commercial", "vlog_story"]
Role = Literal["hook", "body", "broll", "cta", "outro", "filler"]
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clean_text(v: str) -> str:
    return _CTRL.sub(" ", v).strip()


class Reframe(BaseModel):
    mode: Literal["fill", "fit_blur"] = (
        "fill"  # fill = crop to fill frame; fit_blur = whole frame on blurred bg
    )
    focus_x: float = Field(0.5, ge=0.0, le=1.0)  # center of interest, 0..1 of source width
    focus_y: float = Field(0.5, ge=0.0, le=1.0)
    zoom: float = Field(1.0, ge=1.0, le=1.6)  # STATIC zoom for the whole clip (no animation in MVP)


class ClipAudio(BaseModel):
    volume: float = Field(1.0, ge=0.0, le=2.0)
    mute: bool = False


class Transition(BaseModel):
    type: Literal["cut", "crossfade", "fade_black"] = "cut"  # MVP renderer treats everything as "cut"
    duration: float = Field(0.0, ge=0.0, le=1.0)


class Clip(BaseModel):
    id: str = Field(min_length=1, max_length=16)
    src_in: float = Field(ge=0.0)
    src_out: float = Field(gt=0.0)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    role: Role = "body"
    reframe: Reframe = Field(default_factory=Reframe)
    audio: ClipAudio = Field(default_factory=ClipAudio)
    transition_in: Transition = Field(default_factory=Transition)
    note: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _range(self) -> "Clip":
        if self.src_out - self.src_in < 0.3:
            raise ValueError("clip shorter than 0.3 s")
        return self

    @property
    def out_duration(self) -> float:
        return (self.src_out - self.src_in) / self.speed


class Captions(BaseModel):
    enabled: bool = True
    style: Literal["word_highlight", "classic"] = "word_highlight"
    font: Literal["Montserrat-Bold", "NotoSans-Bold", "Inter-Bold"] = "Montserrat-Bold"
    font_size_pct: float = Field(5.0, ge=2.5, le=9.0)  # percent of output height
    position: Literal["top", "middle", "lower_third", "bottom"] = "lower_third"
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    outline_color: str = "#000000"
    max_words_per_line: int = Field(3, ge=1, le=6)
    uppercase: bool = False

    @field_validator("primary_color", "highlight_color", "outline_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _HEX.match(v):
            raise ValueError("color must be #RRGGBB")
        return v.upper()


class Music(BaseModel):
    enabled: bool = False
    track_id: str | None = None  # must exist in assets/music/catalog.json
    volume: float = Field(0.10, ge=0.0, le=0.5)  # linear gain applied to the music track
    ducking: bool = True  # lower music while someone speaks
    fade_out_sec: float = Field(2.0, ge=0.0, le=5.0)


class TextOverlay(BaseModel):
    text: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    position: Literal["top", "middle", "bottom"] = "top"
    style: Literal["title", "cta", "lower_third"] = "title"

    @field_validator("text")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = _clean_text(v)
        if not v:
            raise ValueError("empty text")
        return v

    @model_validator(mode="after")
    def _order(self) -> "TextOverlay":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class Watermark(BaseModel):
    enabled: bool = False
    text: str = Field("", max_length=40)


class Export(BaseModel):
    crf: int = Field(21, ge=17, le=28)
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"] = "veryfast"
    audio_bitrate_k: int = Field(160, ge=96, le=320)
    loudnorm: bool = True
    denoise_audio: bool = False


class Target(BaseModel):
    aspect: Aspect = "9:16"
    fps: int = Field(30, ge=24, le=60)


class EditPlan(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(max_length=80)
    style_preset: StylePreset
    target: Target = Field(default_factory=Target)
    clips: list[Clip] = Field(min_length=1, max_length=400)
    captions: Captions = Field(default_factory=Captions)
    music: Music = Field(default_factory=Music)
    overlays: list[TextOverlay] = Field(default_factory=list, max_length=6)
    watermark: Watermark = Field(default_factory=Watermark)
    export: Export = Field(default_factory=Export)
    human_summary_uz: str = Field("", max_length=1200)

    @model_validator(mode="after")
    def _unique_ids(self) -> "EditPlan":
        ids = [c.id for c in self.clips]
        if len(ids) != len(set(ids)):
            raise ValueError("clip ids must be unique")
        return self

    def total_duration(self) -> float:
        return sum(c.out_duration for c in self.clips)
```

## 2. Output resolutions
`9:16` -> 1080x1920 · `16:9` -> 1920x1080 · `1:1` -> 1080x1080 · `original` -> source size scaled so the short side <= 1080,
both dimensions even. In "light mode" (long videos) and for `RENDER_PRESET=ultrafast`: max short side 720.

## 3. Style presets (`app/services/ai/presets.py`, plain dataclasses)
| key | max_shot_sec | zoom_levels (alternate per clip) | remove_gap_sec | pad_sec | captions | music_volume | target duration | notes |
|---|---|---|---|---|---|---|---|---|
| dynamic_reels | 5 | 1.0, 1.15 | 0.30 | 0.08 | word_highlight, 3 words, Montserrat-Bold, #FFD400 | 0.10 | 15–60 s (or shorter if source shorter) | hook <= 3 s first |
| clean_talk | 12 | 1.0, 1.05 | 0.50 | 0.10 | classic, 5 words | 0.05 (off if no track) | keep most content | minimal effects |
| ad_commercial | 4 | 1.0, 1.10 | 0.30 | 0.08 | word_highlight, 3 words | 0.14 | 15–45 s | hook first; CTA text overlay in last 2–3 s |
| vlog_story | 8 | 1.0, 1.04 | 0.60 | 0.12 | classic, 4 words, optional | 0.08 | keep chronology | never reorder clips |
`remove_gap_sec` = silent gaps longer than this are cut out; `pad_sec` = breathing room kept on both sides of a cut.

## 4. Deterministic post-processing of an LLM plan (order matters)
1. Parse + Pydantic validation (retry the LLM up to 2 times, feeding back the validation error text).
2. `force_job_settings`: set `target.aspect` = job.aspect, `style_preset` = job.style_preset, watermark on for trial jobs,
   captions disabled if the transcript has no words.
3. `snap.snap_cuts(plan, transcript, silences)`: move every `src_in`/`src_out` to the nearest safe point within +-0.35 s:
   prefer the middle of a silence gap; else a word boundary (`src_in` -> word.start - 0.05, `src_out` -> word.end + 0.08);
   never cut inside a word. Keep `src_out - src_in >= 0.3`.
4. `presets.apply_rhythm(plan, transcript, preset)`: split clips longer than `max_shot_sec` at the best sentence/pause boundary
   (>= 1.0 s from clip edges), then assign `reframe.zoom` alternating through `zoom_levels` (skip if source is 16:9 -> 16:9 and
   preset is clean_talk/vlog_story). New clip ids: `c{n}` renumbered sequentially.
5. `plan_validator.validate_plan(plan, source_duration, preset, music_ids)` returns a list of errors; small issues are auto-fixed
   (clamp to source range, drop clips < 0.3 s, unknown music track -> music disabled, overlays clamped to total duration).
   Hard errors: overlapping source ranges (> 0.05 s) between two clips (except vlog_story never reorders), total duration
   > source_duration * 1.05, zero clips. Hard error -> fallback planner.

## 5. Fallback planner (no LLM, always works)
Keep speech, remove silent gaps > `remove_gap_sec` (keep `pad_sec`), merge tiny fragments, apply rhythm, captions on
(preset defaults), music on only if the catalog has a track (first track, preset volume), no overlays, ad_commercial adds
nothing extra. If the source has no speech at all: keep the whole video in scenes marked `usable` by the analysis, no captions.

## 6. Render recipe (deterministic, 3 stages; run every ffmpeg with `cwd=workdir`)
Common: `ffmpeg -y -hide_banner -loglevel error`. `W x H` = output resolution (section 2). `fps` = plan.target.fps.

### Stage A — one file per clip (`clip_0001.mp4`, ...)
Input seeking + re-encode for accuracy:
```
ffmpeg ... -ss {src_in} -t {src_dur} -i SOURCE [-f lavfi -i anullsrc=r=48000:cl=stereo   # ONLY if source has no audio]
  -vf "{reframe_chain},setpts=PTS/{speed},fps={fps},format=yuv420p"
  -af "{atempo},volume={vol},aresample=48000,aformat=channel_layouts=stereo"    # mute -> volume=0; speed==1 -> omit atempo
  -map 0:v:0 -map {0:a:0 | 1:a:0} -t {out_dur}
  -c:v libx264 -preset {preset} -crf {crf} -profile:v high -g {2*fps} -c:a aac -b:a 160k -ar 48000 -ac 2 clip_0001.mp4
```
`src_dur = src_out - src_in`, `out_dur = src_dur / speed`. `atempo={speed}` (range 0.5–2 needs one filter only).
`reframe_chain`:
- `fill`: (cover the target at zoom z, then crop around the focus point; all numbers are computed in Python as integers/floats)
  `scale=w={even(W*z)}:h={even(H*z)}:force_original_aspect_ratio=increase:force_divisible_by=2,`
  `crop={W}:{H}:x='min(max(iw*{fx}-{W}/2,0),iw-{W})':y='min(max(ih*{fy}-{H}/2,0),ih-{H})'`
- `fit_blur` (whole frame visible, blurred copy behind): use `-filter_complex` instead of `-vf`:
  `[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:5[bg];`
  `[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setpts=PTS/{speed},fps={fps},format=yuv420p[v]`
  and map `[v]`. (In overlay expressions write `W`,`H`,`w`,`h` literally: they are ffmpeg variables, not Python.)
  If the source aspect already equals the target aspect, `fill` with zoom 1.0 is just a scale.

### Stage B — join
Write `list.txt` with lines `file 'clip_0001.mp4'` (paths relative, no quotes inside names) then:
`ffmpeg ... -f concat -safe 0 -i list.txt -c copy joined.mp4`
(All clips share codec/resolution/fps/audio params, so `-c copy` is valid.) MVP ignores non-"cut" transitions.

### Stage C — captions/overlays/watermark burn-in + music + loudness
Create `captions.ass` (section 7). Copy or symlink `ASSETS_DIR/fonts` to `workdir/fonts`.
Inputs: `-i joined.mp4` and, if music: `-stream_loop -1 -i MUSIC_FILE`.
Video: `[0:v]ass=captions.ass:fontsdir=fonts[v]` (if no ASS events: `[0:v]null[v]`).
Audio without music: `[0:a]{afftdn=nf=-25,}loudnorm=I=-14:TP=-1.5:LRA=11[a]` (omit loudnorm if `export.loudnorm` false).
Audio with music and ducking:
```
[1:a]volume={mv},afade=t=out:st={total-fade}:d={fade}[m];
[0:a]asplit=2[vo1][vo2];
[m][vo1]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[md];
[md][vo2]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix];
[mix]loudnorm=I=-14:TP=-1.5:LRA=11[a]
```
Without ducking: drop the sidechain and mix `[m]` with `[0:a]` directly. Add `-t {total}` so looped music ends with the video.
Encode: `-map "[v]" -map "[a]" -c:v libx264 -preset {p} -crf {crf} -pix_fmt yuv420p -r {fps} -c:a aac -b:a {ab}k -movflags +faststart final.mp4`.
(`normalize=0` needs ffmpeg >= 4.4; mark `# VERIFY` and fall back to omitting it if the local ffmpeg rejects it.)

## 7. ASS subtitles (`render/captions_ass.py`)
Header: `[Script Info]` `ScriptType: v4.00+`, `PlayResX: {W}`, `PlayResY: {H}`, `WrapStyle: 2`, `ScaledBorderAndShadow: yes`.
Styles (one line each; `Format:` line must list: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour,
Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding):
- `Cap` (captions): Fontsize = `font_size_pct/100*H`, Bold -1, BorderStyle 1, Outline = `0.12*Fontsize`, Shadow 0, MarginL/R = `0.06*W`.
  Alignment (numpad): top=8, middle=5, lower_third=2 with MarginV=`0.18*H`, bottom=2 with MarginV=`0.06*H`.
- `Title`: Alignment 8, size 6% of H. `Cta`: Alignment 2, MarginV `0.30*H`, size 5.5% H, bold. `Lower`: Alignment 1, size 4% H.
- `Wm` (watermark): Alignment 3, size 3% H, PrimaryColour with alpha `&H80...`, MarginV `0.03*H`.
Colors: ASS uses `&HAABBGGRR` (alpha, blue, green, red). Convert `#RRGGBB` -> `&H00BBGGRR`. Inline override: `{\c&H00BBGGRR&}`.
Time format: `H:MM:SS.cc` (centiseconds), computed from OUTPUT timeline seconds.
Timeline mapping: for clip k starting at output offset `O_k`: `t_out = O_k + (t_src - clip.src_in) / clip.speed`.
A word is included in clip k if `word.start >= clip.src_in - 0.05` and `word.end <= clip.src_out + 0.05`; clamp to the clip range.
Grouping: consecutive words form one caption group until `max_words_per_line` is reached, or a pause > 0.6 s, or the clip ends.
- `classic`: one Dialogue per group, text = words joined by spaces, from first word start to last word end.
- `word_highlight`: one Dialogue PER WORD inside the group; each shows the whole group with the current word wrapped in the
  highlight color override and others in primary color. Event i runs from word_i.start to word_{i+1}.start (same group); the last
  word runs to its own end + 0.05. This produces the karaoke-style highlight without ASS `\k` tags.
`uppercase: true` -> `.upper()` the text (locale-aware enough for Latin Uzbek; do not break `o‘`/`g‘` characters).

## 8. Text safety (mandatory)
All user-visible text (captions from STT, overlay text, watermark text, title) is inserted into ASS by `escape_ass(text)`:
1. remove control characters; 2. replace `\` with `/`; 3. remove `{` and `}`; 4. collapse whitespace; 5. never allow the text to start an
override block. Line breaks are generated by our code as `\N` only. NEVER put such text into `-vf`/`-filter_complex` strings or file
paths. Overlay and watermark text length limits are enforced by the schema. Tests must cover `{\pos(0,0)}` and `\N` injection attempts.

## 9. Music catalog
`assets/music/catalog.json`: `[{"id":"upbeat_01","title":"...","mood":"upbeat","file":"upbeat_01.mp3","license":"...","bpm":120}]`.
Only royalty-free tracks the owner has the right to use commercially. Planner receives the list of `id`+`mood`. If the catalog is empty,
music is always disabled. NEVER download or embed copyrighted music.
