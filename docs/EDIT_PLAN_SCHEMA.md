# EditPlan — schema, rules, presets and render recipe

The `EditPlan` is the ONLY contract between the AI planner and the renderer. The LLM produces JSON that must validate
against this schema. The renderer trusts only validated plans.

Time bases: `clip.src_in/src_out` = seconds in the clip's SOURCE video (`clip.source_id`: `"primary"` = the job's main
upload, `"broll_N"` = an attached B-roll upload). `overlay.start/end` = seconds in the OUTPUT timeline.
Source geometry everywhere (planner input, output sizing, reframing) is DISPLAY geometry: rotation flags and non-square
pixels are already applied by `probe()`.

## 1. Pydantic models (the real code is `app/schemas/edit_plan.py`; keep this in sync)
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
    zoom: float = Field(1.0, ge=1.0, le=1.6)  # start zoom; "fill" also pushes in ~8% (Ken Burns)


class ClipAudio(BaseModel):
    volume: float = Field(1.0, ge=0.0, le=2.0)
    mute: bool = False
    # B-roll dub: "primary" takes this clip's audio from the job's PRIMARY source at primary_src_in/out, so the
    # narration keeps playing under a (silent, picture-only) B-roll clip. Required window when source == "primary".
    source: Literal["own", "primary"] = "own"
    primary_src_in: float | None = Field(None, ge=0.0)
    primary_src_out: float | None = Field(None, gt=0.0)

    @model_validator(mode="after")
    def _primary_window(self) -> "ClipAudio":
        if self.source == "primary":
            if self.primary_src_in is None or self.primary_src_out is None:
                raise ValueError("audio.source 'primary' needs primary_src_in and primary_src_out")
            if self.primary_src_out <= self.primary_src_in:
                raise ValueError("primary_src_out must be after primary_src_in")
        return self


class Transition(BaseModel):
    # cut = hard cut; crossfade = short plain cross-dissolve; fade_black = dip to black. Applied to the cut INTO this clip.
    type: Literal["cut", "crossfade", "fade_black"] = "cut"
    duration: float = Field(0.0, ge=0.0, le=1.0)


class Clip(BaseModel):
    id: str = Field(min_length=1, max_length=16)
    source_id: str = Field("primary", min_length=1, max_length=16)  # "primary" or a broll_sources id
    src_in: float = Field(ge=0.0)
    src_out: float = Field(gt=0.0)
    speed: float = Field(1.0, ge=0.5, le=2.0)
    # speed curve inside the clip; the AVERAGE stays `speed`, so out_duration is unchanged (see Stage A)
    speed_ramp: Literal["none", "fast_to_slow", "slow_to_fast"] = "none"
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
    style: Literal["word_highlight", "pop", "karaoke", "classic"] = "word_highlight"
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


class Sfx(BaseModel):
    enabled: bool = False  # whooshes on scene changes, pops on text overlays (placed by the renderer)
    volume: float = Field(0.5, ge=0.0, le=1.0)


class Music(BaseModel):
    enabled: bool = False
    track_id: str | None = None  # must exist in assets/music/catalog.json
    volume: float = Field(0.10, ge=0.0, le=0.5)  # linear gain applied to the music track
    ducking: bool = True  # lower music while someone speaks
    fade_out_sec: float = Field(2.0, ge=0.0, le=5.0)
    beat_sync: bool = True  # start the track where the cuts land on its beat


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
    look: Literal["natural", "warm", "cool", "cinematic", "vivid", "bw", "vintage"] = "natural"
    sfx: Sfx = Field(default_factory=Sfx)
    overlays: list[TextOverlay] = Field(default_factory=list, max_length=6)
    stickers: list[Sticker] = Field(default_factory=list, max_length=8)  # emoji names from render/stickers.py
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
   captions disabled if the transcript has no words. An overlay sitting in the captions' screen zone is moved to the opposite
   side (two texts in one zone look cluttered).
3. `snap.snap_cuts(plan, transcript, silences)`: move every PRIMARY-source `src_in`/`src_out` to the nearest safe point within
   +-0.35 s: prefer the middle of a silence gap; else a word boundary (`src_in` -> word.start - 0.05, `src_out` -> word.end + 0.08);
   never cut inside a word. Keep `src_out - src_in >= 0.3`. B-roll clips are left untouched (the transcript is timed against the
   primary file only); overlap undoing is done per `source_id`.
4. `plan_validator.apply_rhythm(plan, transcript, preset, source_width, source_height)`: split PRIMARY clips longer than `max_shot_sec`
   at the best sentence/pause boundary (>= 1.0 s from clip edges), then assign `reframe.zoom` alternating through `zoom_levels`
   (skip if source is 16:9 -> 16:9 and preset is clean_talk/vlog_story). A small square source (a Telegram round video note,
   `probe.looks_like_round_video_note`) is forced to `fit_blur` instead. Then every REAL editorial cut gets
   `transition_in = crossfade(preset.crossfade_sec)`; a cut inside one continuous take (next clip starts where the previous ended,
   same source and speed) or at a voice-continuous B-roll boundary stays a hard cut (a dissolve there swallows content).
   New clip ids: `c{n}` renumbered sequentially.
5. `plan_validator.validate_plan(plan, source_duration, preset, music_ids, broll_durations)` returns a list of errors; small issues
   are auto-fixed (clamp to the clip's own source range, drop clips < 0.3 s, unknown music track -> music disabled, overlays clamped
   to total duration, an invalid `audio.primary_src_*` window clamps or falls back to a muted clip).
   Hard errors: a clip whose `source_id` is not `primary`/a known B-roll, overlapping ranges (> 0.05 s) between two clips OF THE SAME
   source (vlog_story never reorders; with B-roll present the chronological sort is skipped), total duration
   > (primary + B-roll durations) * 1.05, zero clips. Hard error -> fallback planner.

## 5. Fallback planner (no LLM, always works)
Keep speech, remove silent gaps > `remove_gap_sec` (keep `pad_sec`), merge tiny fragments, apply rhythm, captions on
(preset defaults), music on only if the catalog has a track (first track, preset volume), no overlays, ad_commercial adds
nothing extra. If the source has no speech at all: keep the whole video in scenes marked `usable` by the analysis, no captions.

## 6. Render recipe (deterministic, 3 stages; run every ffmpeg with `cwd=workdir`)
Common: `ffmpeg -y -hide_banner -loglevel error`. `W x H` = output resolution (section 2). `fps` = plan.target.fps.

### Stage A — one file per clip (`clip_0001.mp4`, ...)
Implemented in `render/clip_stage.py` (this section describes the contract; that file has the exact filters). Per source, `engine.py`
probes once and passes `hdr`, `sar`, `round_note` to every clip. Filters that run BEFORE reframing, in this order:
HDR (PQ/HLG) -> SDR BT.709 tone-map (`zscale npl=203` + `mobius`, calibrated on real signal levels: the common `npl=100` + `hable`
recipe renders diffuse white visibly dark) · SAR -> square pixels · optional `deshake` (`RENDER_STABILIZE`, off) · light colour polish

**Face-aware framing.** For `reframe.mode == "fill"` (not round video notes) `face_track.py` samples the clip at 4 fps, detects faces with YuNet (`assets/models/*.onnx`), follows one subject and turns the detections into a smoothed camera path (dead zone, easing, speed cap). `clip_stage.fill_chain` writes it as a piecewise-linear expression of `t` in the crop x/y, replacing the static `focus_x/focus_y`. Any failure (no model, no face, <35% detections) falls back to the static focus. Disable with `RENDER_FACE_TRACKING=false`. **Active speaker:** with several faces the tracker links detections into tracks and measures each mouth's shape change
between samples (patch from YuNet's mouth-corner landmarks, remembered across short detector dropouts). The subject is the
largest face until another track's smoothed mouth activity beats it by 1.6x for 0.75 s; the switch is backdated to when that
person started talking, and a switch farther than half a crop width is a hard cut (two keys 20 ms apart), a nearer one a pan.
This is visual (lip motion) speaker detection, not audio diarization: it needs visible, roughly frontal mouths.
(`eq` contrast/saturation + luma-only `unsharp`). `fill` clips also get the slow Ken Burns push-in (crop window shrinks ~8% over the
clip). `fit_blur` on a round video note first crops the largest square inside the circle (`0.68 * min(w,h)`), so the baked-in mask
corners never show, then shows it full-width over a darkened blur of itself. Audio ends with `apad` (never underruns the video);
`audio.source = "primary"` maps the audio of a SECOND input (the primary file, `-ss primary_src_in -t ...`) instead of the clip's own.
The recipe below is the original minimal form:
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

**Speed ramp.** `retime(clip)` replaces `setpts=PTS/speed`: speed changes linearly in source time from s0 to s1
(`speed*1.6` <-> `speed*RAMP_SLOW`), i.e. `setpts='K*log(1+c*(T-STARTT))/TB'`. `RAMP_SLOW` (~0.57) is solved so the
average is exactly 1x - the clip's output length, captions and B-roll dub windows are unaffected. The validator resets
ramps on clips whose own sound is heard (it would warp the voice), and `apply_rhythm` never splits a ramped clip.
A B-roll dub (`audio.source == "primary"`) plays its narration window at 1x (no `atempo`), and captions show the words
of that primary window; muted clips and B-roll with its own sound get no captions.

### Stage B — join (`render/concat_stage.py`)
All-cut plans: write `list.txt` with lines `file 'clip_0001.mp4'` (paths relative, no quotes inside names) then
`ffmpeg ... -f concat -safe 0 -i list.txt -c copy joined.mp4` (all clips share codec/resolution/fps/audio params, so `-c copy` is valid).
As soon as one clip has a `crossfade`/`fade_black` transition with duration > 0, the join is a `filter_complex` of `xfade` +
`acrossfade` (`crossfade` = xfade `fade`, a plain cross-dissolve; `fade_black` = `fadeblack`) and hard cuts as `concat` pairs.
Rules that keep it correct (each has a real-ffmpeg test):
- offsets come from the PROBED durations of the rendered clip files, not the plan; overlap = min(duration, both neighbours);
- every video input first gets `settb=AVTB`: `concat` outputs timebase 1/1000000 and mp4 inputs 1/15360, and `xfade` refuses mixed timebases;
- ffmpeg opens every input at once, so at most `MAX_XFADE_INPUTS = 5` inputs are joined per graph: groups first (near-lossless
  intermediates), then the group files with the group's FIRST clip's `transition_in` - the timeline math equals one flat chain,
  and peak memory stays about 1 GB regardless of clip count.

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

**Beat sync** (`render/beats.py`, `music.beat_sync`, default on). Cuts are never moved (they sit on word boundaries).
The track's beat grid is detected (spectral-flux onsets, autocorrelation tempo with a 120 BPM prior, then a joint fine
tempo/phase comb search; onset latency calibrated on click tracks: tempo +-0.15 BPM, phase +-12 ms), and the music starts
at the offset where the most cuts (scene changes weighted x2) fall within 70 ms of a beat:
`[1:a]atrim=start={o},asetpts=PTS-STARTPTS,afade=t=in:d=0.25,volume=...`. No steady pulse / analysis error -> offset 0.

**Look.** `plan.look` names one fixed colour grade from `render/looks.py` (colorbalance/curves/eq/vignette chains written
in code, never by the AI); it runs in front of `ass` so text keeps its exact colours. `natural` = no filter.

**Stickers** (`render/stickers.py`). `plan.stickers[]` names emoji from a fixed catalog (Noto Emoji PNGs in
`assets/emoji`, Apache-2.0); unknown names are dropped by the schema, the validator keeps at most 4 inside the video,
never two at the same spot at once, and `force_job_settings` moves them out of the caption band. Each is an extra
`-loop 1 -t {dur} -i png` input: `format=rgba,scale=(bounce: 0.35 -> 1.15 -> 1.0 in 0.22 s, eval=frame),fade out,
setpts=PTS+start/TB` overlaid at the side (`top_*` at 20% height, `middle_*` at 45%) - order: look -> stickers -> ASS text.

**Sound effects** (`render/sfx.py`, when `plan.sfx.enabled`). Synthesised by ffmpeg (`anoisesrc` whoosh, `aevalsrc` pop) -
no sample files, nothing to license. Placement is code: a whoosh whose swell peaks on each *structural* cut (non-cut
transition, source change, or a role change involving hook/cta/outro/broll), a plain jump cut only if the previous cue is
>= 3 s old, a pop at each overlay start; cues >= 1.4 s apart, max 40. They are mixed AFTER loudnorm of the voice/music
(`...loudnorm...[voice]; fx...; [voice][fx0]..amix=normalize=0[mixfx]; [mixfx]alimiter=limit=0.84:level=0[a]`), so their
level relative to speech is fixed: at volume 0.4 a whoosh peaks ~-20 LUFS momentary against -14 LUFS speech.
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
- `pop` (default of `dynamic_reels`): like `word_highlight`, but the active word bounces in (82% -> 118% -> 100%, ASS `\t` transforms).
- `karaoke`: one Dialogue per group; each word sweeps to the highlight colour while spoken (`\kf`, style `Kar`).
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
