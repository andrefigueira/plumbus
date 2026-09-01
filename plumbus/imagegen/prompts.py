"""Prompt scaffolding for reference images that photogrammetry-style models like.

A good 3D reference is not a good picture. Depth of field, dramatic rim light,
cast shadows and a cropped subject all become geometry errors downstream, so the
preset strips them out and states the camera explicitly.
"""
from __future__ import annotations

VIEWS: dict[str, str] = {
    "three_quarter": (
        "three-quarter view from slightly above eye level, showing the front "
        "and one side of the subject"
    ),
    "front": "straight-on front orthographic view, subject facing the camera squarely",
    "back": "straight-on rear orthographic view, subject facing directly away",
    "left": "straight-on left profile view, subject facing exactly to the camera's left",
    "right": "straight-on right profile view, subject facing exactly to the camera's right",
    "top": "top-down orthographic view looking straight down at the subject",
}

VIEW_LABELS: dict[str, str] = {
    "three_quarter": "Three-quarter",
    "front": "Front",
    "back": "Back",
    "left": "Left profile",
    "right": "Right profile",
    "top": "Top down",
}

# Ordered so the first N of a multi-view set are the most informative.
MULTIVIEW_ORDER = ["three_quarter", "front", "left", "back"]

_GEOMETRY_RULES = (
    "The entire subject is visible with clear margin on all sides, nothing cropped "
    "or cut off at any edge. One single object only, centred in frame. "
    "Flat even diffuse studio lighting from all directions, no harsh shadows, "
    "no cast shadow on the ground, no dramatic rim light, no strong specular hotspots. "
    "Everything in sharp focus edge to edge, no depth of field, no bokeh, no motion blur. "
    "Long lens with minimal perspective distortion, near-orthographic. "
    "Neutral colour, no colour grading, no vignette, no lens flare. "
    "No text, no watermark, no logo, no measurement callouts, no scale figure, "
    "no props, no hands, no ground plane, no pedestal."
)

_BG_TRANSPARENT = "The background is fully transparent."
_BG_FLAT = "The background is a flat, uniform, neutral mid-grey, completely empty."

_OBJECT_DETAIL_RULES = (
    "Preserve physically plausible construction, crisp part boundaries, bevels, seams, "
    "fasteners, surface wear and fine material variation. Micro-detail must remain sharp "
    "and readable without exaggerating it into lumpy geometry."
)

_CHARACTER_RULES = (
    "Show the complete character in a neutral symmetrical A-pose suitable for character "
    "modelling: standing upright, feet shoulder-width apart and parallel, arms about 35 "
    "degrees away from the torso, elbows and knees straight but not locked. Hands are open "
    "with relaxed, clearly separated fingers and thumbs. The face has a neutral expression, "
    "eyes open looking straight ahead, lips gently closed, ears and jawline visible. Preserve "
    "realistic anatomy, facial asymmetry, eyelids, nostrils, lip volume, nail beds, skin pores, "
    "cloth weave, seams and material response. Keep hair and clothing from bridging the neck, "
    "armpits, wrists, fingers, crotch, knees or ankles. No dynamic pose and no facial expression."
)


def _profile_rules(profile: str) -> str:
    return _CHARACTER_RULES if profile == "character" else _OBJECT_DETAIL_RULES


def build(
    subject: str,
    view: str = "three_quarter",
    transparent: bool = True,
    profile: str = "object",
) -> str:
    """Wrap a plain subject description in the constraints that survive to geometry."""
    subject = subject.strip().rstrip(".")
    camera = VIEWS.get(view, VIEWS["three_quarter"])
    background = _BG_TRANSPARENT if transparent else _BG_FLAT
    return (
        f"A single {subject}, photographed as a clean 3D modelling reference. "
        f"Camera: {camera}. "
        f"{background} "
        f"{_GEOMETRY_RULES} {_profile_rules(profile)}"
    )


def build_view_edit(
    subject: str,
    view: str,
    transparent: bool = True,
    profile: str = "object",
) -> str:
    """Prompt for re-shooting an existing reference from another angle.

    Used against the edits endpoint with the first view attached, so the model
    has the actual subject rather than a description of it.
    """
    subject = subject.strip().rstrip(".")
    camera = VIEWS.get(view, VIEWS["front"])
    background = _BG_TRANSPARENT if transparent else _BG_FLAT
    return (
        f"Redraw the exact same {subject} from the attached reference image, "
        f"unchanged in shape, proportion, colour, material and every detail. "
        f"Only the camera angle changes. Camera: {camera}. "
        f"{background} {_GEOMETRY_RULES} {_profile_rules(profile)}"
    )
