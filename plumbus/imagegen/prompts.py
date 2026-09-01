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

# A head has no use for a rear view: Mesh to MetaHuman explicitly tolerates a
# rough skull but wants forehead-to-chin and both ears observed properly.
HEAD_MULTIVIEW_ORDER = ["front", "left", "right", "three_quarter"]

# Everything a profile might want to switch on. The schema offers all of these
# and each profile picks its own defaults, so a head can ask for both profiles
# without the object flow having to care.
SELECTABLE_VIEWS = ["three_quarter", "front", "left", "right", "back"]

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


_HEAD_RULES = (
    "Framing: head and shoulders only, filling the frame from the top of the head to the "
    "collarbone. Nothing below the shoulders. "
    "The head is upright and the neck straight, facing the camera squarely for the front view. "
    "Completely neutral, relaxed expression, no smile, mouth closed with the lips lightly "
    "together and the jaw unclenched. Both eyes open, wide and unsquinting, looking straight "
    "ahead, with the upper eyelid clear of the iris. "
    "Both ears fully visible and unobstructed, hair tucked behind them or pulled back off the "
    "face. No hair across the forehead, eyes, cheeks or jawline. "
    "No glasses, no headwear, no hood, no mask, no jewellery, no piercings, nothing covering "
    "any part of the face. No makeup and no face paint. "
    "Critically, the face carries no shadow of any kind: no shadow in the eye sockets, none "
    "under the brow, none under the nose, none under the lower lip, none under the chin or "
    "jaw, and none cast by the hair. Light the face flatly and evenly from directly in front. "
    "No specular highlight or catchlight in the eyes, on the nose, forehead or cheekbones. "
    "Skin reads as skin: pores, fine lines, stubble and natural colour variation are visible, "
    "but flat and unshaded, never glossy, wet or waxy. "
    "Facial hair is part of the face and is fully preserved: any beard, moustache, stubble and "
    "the eyebrows stay at their real length, colour and density, rendered as a solid, clearly "
    "bounded mass, not wispy single strands. "
    "Below the neck the subject wears only a plain, fitted, matte light-grey crew-neck t-shirt, "
    "whatever they were wearing before: no coat, no collar, no hood bunched at the neck, no "
    "scarf, no jewellery. The shoulders exist to give the scan a clean base, nothing more."
)


def _profile_rules(profile: str) -> str:
    if profile == "head":
        return _HEAD_RULES
    if profile == "character":
        return _CHARACTER_RULES
    return _OBJECT_DETAIL_RULES


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
    lead = (
        f"The head of {subject}, photographed as a clean 3D head-scan reference. "
        if profile == "head"
        else f"A single {subject}, photographed as a clean 3D modelling reference. "
    )
    return (
        f"{lead}"
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
    if profile == "head":
        # A head close-up exists for the FACE, so identity is preserved but
        # face-occluding garments are not: a hat brim shades the brow and hides
        # the hairline in every view, and what the engine cannot observe it
        # shreds. The body's own views keep the outfit; this one loses it.
        return (
            f"Redraw the head and shoulders of the exact same {subject} from the "
            f"attached reference image: identical face, identity, age, bone "
            f"structure, skin, eyes and hair colour in every detail, and exactly "
            f"the same facial hair, the same beard, moustache, stubble and "
            f"eyebrows at the same length, colour and density. Remove only "
            f"garments and accessories covering the head or face: no hat, no "
            f"hood, no helmet, no glasses, no mask, no scarf, with the scalp "
            f"hair pulled back off the face and both ears fully visible. "
            f"Camera: {camera}. {background} {_GEOMETRY_RULES} {_HEAD_RULES}"
        )
    return (
        f"Redraw the exact same {subject} from the attached reference image, "
        f"unchanged in shape, proportion, colour, material and every detail. "
        f"Only the camera angle changes. Camera: {camera}. "
        f"{background} {_GEOMETRY_RULES} {_profile_rules(profile)}"
    )
