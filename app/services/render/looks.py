"""Colour looks: one named grade applied to the whole video in the final stage (CapCut-style "filters").

Every look is a fixed ffmpeg filter chain written here; the plan only names it, so the AI can never inject
filter syntax. `natural` is the identity (the per-clip `color_polish` still applies to every look)."""

from typing import Literal, get_args

Look = Literal["natural", "warm", "cool", "cinematic", "vivid", "bw", "vintage"]
LOOKS: tuple[str, ...] = get_args(Look)

_CHAINS: dict[str, str] = {
    "natural": "",
    "warm": "colorbalance=rs=0.05:gs=0.02:bs=-0.06:rm=0.05:bm=-0.05:rh=0.03:bh=-0.03,eq=saturation=1.06",
    "cool": "colorbalance=rs=-0.04:bs=0.06:rm=-0.04:bm=0.05:bh=0.03,eq=saturation=1.03",
    # teal shadows / warm highlights, deeper contrast, soft vignette
    "cinematic": (
        "curves=preset=increase_contrast,colorbalance=rs=-0.05:bs=0.07:rh=0.07:bh=-0.05,"
        "eq=saturation=0.94:contrast=1.04,vignette=angle=PI/6"
    ),
    "vivid": "eq=contrast=1.08:saturation=1.32,unsharp=lx=5:ly=5:la=0.3:cx=5:cy=5:ca=0.0",
    "bw": "hue=s=0,eq=contrast=1.18:brightness=-0.02",
    "vintage": "curves=preset=vintage,eq=saturation=0.88:contrast=1.03",
}


def look_filter(look: str) -> str:
    """The filter chain for `look` ("" for natural). Unknown names fall back to natural."""
    return _CHAINS.get(look, "")
