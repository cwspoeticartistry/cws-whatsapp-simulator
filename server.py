"""
CWS WhatsApp Simulator — Local Webhook Server
-----------------------------------------------
Flask server connecting the WhatsApp simulator to the existing image generation workflow.

Endpoints:
  GET  /                        — Server status
  GET  /health                  — Health check
  POST /webhook                 — Main chat webhook (processes messages)
  GET  /api/catalogue           — Full product catalogue with image URLs
  GET  /api/product-image/<fn>  — Serves product images
  GET  /api/generated/<fn>      — Serves generated images

Usage:
    "C:\\Users\\Dell\\Documents\\Website Development\\.venv\\Scripts\\python.exe" server.py
"""

import base64
import json
import os
import queue as _queue_mod
import re
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests as _requests
from flask import Flask, request, jsonify, send_from_directory, send_file, Response
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(r"C:\Users\Dell\Documents\Dad\Promo Images")
SKILL_ROOT = Path(r"C:\Users\Dell\.claude\skills\social-image-prompter")
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REFS_DIR = SKILL_ROOT / "references"
PRODUCTS_DIR = PROJECT_ROOT / "products"
GENERATED_DIR = Path(__file__).parent / "generated"
ENV_PATH = Path(r"C:\Users\Dell\Documents\Website Development\.env")
CWS_SHARED = Path(r"C:\Users\Dell\.claude\skills\cws-shared")
BUSINESS_STATE_DIR = CWS_SHARED / "state"
OPERATIONS_LOG = CWS_SHARED / "logs" / "operations.jsonl"

sys.path.insert(0, str(SCRIPTS_DIR))

# ---------------------------------------------------------------------------
# Load env
# ---------------------------------------------------------------------------
def load_env():
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip("\"'"))

load_env()

# ---------------------------------------------------------------------------
# Scene analyser — OpenRouter / Gemini Flash
# ---------------------------------------------------------------------------
OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"
SCENE_ANALYSIS_MODEL = "google/gemini-2.0-flash-001"

SCENE_ANALYSIS_SYSTEM = """You are a professional art director and image generation prompt engineer.
Your job is to analyse a reference photograph and produce a precise scene brief that will be used
to guide an AI image generator (kie.ai) to recreate the environment while swapping in a new product.

IMPORTANT: The image may be a screenshot from a phone or social media app. Completely IGNORE any
mobile UI chrome — status bars, navigation bars, like/share/comment buttons, follower counts, usernames,
app interfaces, captions, or any overlay that is not part of the actual photograph. Analyse ONLY the
photographic content itself — the background, lighting, surfaces, props, and scene composition.

Output ONLY a JSON object with these keys — no markdown, no commentary:
{
  "background": "detailed description of the background/setting (room, outdoor, surface, walls, etc.)",
  "lighting": "light direction, quality, colour temperature, shadows",
  "surfaces": "what the product sits or is placed on (marble counter, wooden table, etc.)",
  "atmosphere": "overall mood, colour palette, dominant hex colors, style (rustic, modern, clinical, warm, etc.)",
  "subjects": "any people, hands, or living subjects visible and their position/action",
  "props": "any other objects in the scene besides the main product",
  "keep": "comma-separated list of elements that MUST be preserved exactly in the new image",
  "change": "comma-separated list of elements the user wants replaced or modified",
  "scene_prompt": "a single flowing paragraph (60-80 words) written as an image generation prompt that captures the full environment with specific colors, textures, and composition — do NOT mention any specific product, brand, or person by name"
}"""

def analyze_scene_image(b64_data: str, mime_type: str, user_request: str) -> dict | None:
    """
    Call OpenRouter (Gemini Flash) with the uploaded reference image + user request.
    Returns a parsed dict of the scene brief, or None on failure.
    Model: google/gemini-2.0-flash-001 — fast, cheap, excellent vision.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("  [Scene Analyser] OPENROUTER_API_KEY not set — skipping analysis")
        return None

    # Strip data URI prefix if present
    if "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]

    user_message = (
        f"Analyse this reference photograph. The user's request is: \"{user_request or 'place a product in this scene'}\".\n"
        f"Based on the image and the user's request, identify what to keep and what to change.\n"
        f"Return the JSON object as specified."
    )

    payload = {
        "model": SCENE_ANALYSIS_MODEL,
        "messages": [
            {"role": "system", "content": SCENE_ANALYSIS_SYSTEM},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}
                    },
                    {"type": "text", "text": user_message},
                ],
            },
        ],
        "max_tokens": 600,
        "temperature": 0.3,
    }

    try:
        resp = _requests.post(
            OPENROUTER_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://cwspoeticartistry.github.io/cws-whatsapp-simulator/",
                "X-Title": "CWS Dr Gee Scene Analyser",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if model wraps the JSON
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
        result = json.loads(raw)
        print(f"  [Scene Analyser] Analysis complete — keep: {result.get('keep','?')[:60]}")
        return result
    except json.JSONDecodeError as e:
        print(f"  [Scene Analyser] JSON parse error: {e} | raw: {raw[:200]}")
    except Exception as e:
        print(f"  [Scene Analyser] Error: {e}")
    return None


# ---------------------------------------------------------------------------
# Product image mapping
# ---------------------------------------------------------------------------
IMAGE_MAP = {
    "Blood Circulations": "Herbal Vitality & Flow Support - Blood Circulation.png",
    "Blood Purifying": "Herbal Vitality Blend - Blood Purification.png",
    "Bones, Joints and Gout": "Herbal Duo Comfort Blend - Bones, Joints and Gout.png",
    "Cholesterol Control": "Herbal Clarity Blend - Cholesterol Control.png",
    "Asthma & Lung Repair": "Asthma & Lung Repair.psd.png",
    "Black Seed Oil Capsules": "Dr Gee Black Seed Capsules.psd.png",
    "Dr Gee Omega 3": "Omega-3 Softgel Capsules.psd.png",
    "Iron Essence Capsules": "Iron essence capsules.psd.png",
    "Joint Harmony Capsules": "Daily Joint Comfort - Joint Harmony Capsules.psd.png",
    "Magnesium Complex": "Magnesium Complex - Herbal Mineral Blend.psd.png",
    "QS-8 with Nano Technology": "With Nano Technology.psd.png",
    "QS8 Daily Support": "QS8 Daily Support - QS 8 Capsules.psd.png",
    "Restore Plus": "Daily Balance Capsules - Restore Plus.psd.png",
    "RPG 4 FLU": "RPG 4 FLU - Herbal Energy Blend.psd.png",
    "Shilajit Capsules": "Shilajit Capsules.psd.png",
    "Sleep Aid": "Sleep Aid - Herbal Relaxation Blend.psd.png",
    "Soursop Capsules": "Soursop Capsules.psd.png",
    "Detox": "Herbal Vitality Powder - Detox.psd.png",
    "GERD Relief Powder": "Herbal Ease Powder - GERD Relief Powder.psd.png",
    "Liver & Kidney Tonic Powder": "Herbal Vitality Powder - Detox.psd.png",
    "Man's Powder": "Men's Herbal Powder - Man's Powder.psd.png",
    "Sugar Ease": "Sugar Ease - Herbal Harmony Blend.psd.png",
    "Advanced Kidney, Liver & Bladder": "Advanced Herbal Vitality Syrup - Advanced Kidney, Liver & Bladder.psd.png",
    "Eye and Ear Drops": "Botanical Drops - Eye and Ear Drops.psd.png",
    "Kidney Liver and Bladder Tincture": "Herbal Renewal Tonic - Kidney, Liver and Bladder Tincture.psd.png",
    "QS 8 Spray": "QS 8 Spray.psd.png",
    "QS-8 Nasal Spray": "QS 8 Nasal Spray.psd.png",
    "QS 8 Throat Spray": "QS 8 Throat Spray.psd.png",
    "Herbal Boost Blend": "Herbal Vitality Tonic - Herbal Boost Blend.psd.png",
    "Libido Tonic": "Herbal Wellness Tonic - Libido Tonic.psd.png",
    "Chromium Glucobalance": "Chromium Glucobalance - Daily Metabolic Balance.psd.png",
    "QS7 Syrup (Kalonji / Black Seed Oil)": "QS 7 Syrup.psd.png",
    "Man's Soup": "Men's Herbal Soup Blend - Man's Soup.psd.png",
    "Ulcer Solution": "Ulcer Solution - Herbal Comfort Blend.psd.png",
    "Ulcer Solutions": "Ulcer Solutions.psd.png",
    "Herbal Vitality & Flow Support 2": "Herbal Vitality & Flow Support - Blood Circulation 2.png",
    "Herbal Vitality Blend 2": "Herbal Vitality Blend - Blood Purification 2.png",
    "Corrective for Women": "Women's Wellness Herbal Tonic -Corrective for Women.png",
    "Blood Combo": "Blood Combo.png",
    "Bones & Joint Combo": "Bones & Joint Combo.png",
    "Detox, Bones and Joints Combo": "Detox, Bones and Joints Combo.png",
    "Liver and Kidney Combo": "Liver and Kidney Combo.png",
    "Men's Combo": "Mens Combo.png",
    "Piles Combo": "Piles Combo.png",
    "Woman's Combo": "Womens Combo.png",
}

# Map old/file names to catalogue names for display
OLD_NAMES = {
    "Blood Circulations": "Herbal Vitality & Flow Support",
    "Blood Purifying": "Herbal Vitality Blend",
    "Advanced Kidney, Liver & Bladder": "Levels 3 / Advanced Herbal Vitality Syrup",
    "QS-8 with Nano Technology": "With Nano Technology",
    "Iron Essence Capsules": "Iron essence capsules",
}


def find_image_for_product(name):
    """Find the product image filename for a given product name."""
    if name in IMAGE_MAP:
        fn = IMAGE_MAP[name]
        if (PRODUCTS_DIR / fn).exists():
            return fn
        # Path check failed (possible encoding/case issue) — do case-insensitive scan
        fn_lower = fn.lower()
        for f in PRODUCTS_DIR.iterdir():
            if f.name.lower() == fn_lower:
                return f.name
    # Fuzzy fallback — check for partial match in filenames
    name_lower = name.lower()
    for f in PRODUCTS_DIR.iterdir():
        if f.suffix.lower() in ('.png', '.jpg', '.jpeg'):
            if name_lower in f.stem.lower() or any(w in f.stem.lower() for w in name_lower.split() if len(w) > 3):
                return f.name
    return None


# ---------------------------------------------------------------------------
# Load catalogue and cache
# ---------------------------------------------------------------------------
def load_catalogue():
    cat_path = REFS_DIR / "dr-gee-catalogue.md"
    if not cat_path.exists():
        return []
    products = []
    current = {}
    with open(cat_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("### "):
                if current.get("name"):
                    products.append(current)
                current = {"name": line[4:].strip()}
            elif line.startswith("- **Product line:**"):
                current["product_line"] = line.split(":**")[1].strip()
            elif line.startswith("- **SKU:**"):
                current["sku"] = line.split("`")[1] if "`" in line else ""
            elif line.startswith("- **Price:**"):
                price_str = line.split("R")[1].strip() if "R" in line else "0"
                current["price"] = int(re.sub(r"[^\d]", "", price_str.split()[0]))
            elif line.startswith("- **Short description:**"):
                current["description"] = line.split(":**")[1].strip()
            elif line.startswith("- **Image available:**"):
                current["image_available"] = "Yes" in line
            elif line.startswith("- **Label text:**"):
                current["label_text"] = line.split(":**")[1].strip()
            elif line.startswith("- **Variant:**"):
                current["variant"] = line.split(":**")[1].strip()
            elif line.startswith("- **Components:**"):
                current["components"] = line.split(":**")[1].strip()
    if current.get("name"):
        products.append(current)
    return products


def load_cache():
    cache_path = REFS_DIR / "product-prompt-cache.json"
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def find_product(text, catalogue):
    text_lower = text.lower().strip()
    for p in catalogue:
        if p["name"].lower() in text_lower:
            return p
    text_words = set(re.findall(r"\w+", text_lower))
    best_score = 0
    best_match = None
    for p in catalogue:
        name_words = set(re.findall(r"\w+", p["name"].lower()))
        name_words -= {"and", "the", "for", "with", "of", "a", "in"}
        overlap = len(text_words & name_words)
        if overlap > best_score and overlap >= 1:
            best_score = overlap
            best_match = p
    return best_match


_STOP_WORDS = {"and", "or", "the", "a", "of", "for", "with", "in", "to", "by", "at"}

def find_all_products(text: str, cat: list) -> list:
    """Return every product whose name OR product_line appears in text (case-insensitive).
    Matching strategies (any one sufficient):
      1. Exact substring: product name in text
      2. Product_line substring (with parenthetical suffix stripped)
      3. Word-overlap >= 75% for names/lines with 3+ significant words
    """
    t = text.lower()
    t_words = set(re.findall(r"\w+", t)) - _STOP_WORDS
    found = []
    seen = set()
    for p in cat:
        name = p["name"].lower()
        pl = (p.get("product_line") or "").lower()
        # Strip parenthetical suffixes like "(Capsules)", "(Powder)" for cleaner matching
        pl_clean = re.sub(r"\s*\([^)]*\)", "", pl).strip()

        # Strategy 1 & 2: substring match
        matched = (
            name in t
            or (len(pl_clean) > 5 and pl_clean in t)
            or (len(pl) > 5 and pl in t)
        )

        # Strategy 3: word-overlap for multi-word names (catches word-order variants)
        if not matched:
            name_words = set(re.findall(r"\w+", name)) - _STOP_WORDS
            if len(name_words) >= 3:
                overlap = len(name_words & t_words)
                if overlap / len(name_words) >= 0.75:
                    matched = True

        if matched and p["name"] not in seen:
            found.append(p)
            seen.add(p["name"])
    return found


# Intent constants
_INTENT_SINGLE       = "single"
_INTENT_MULTI        = "multi"        # multiple products → separate images
_INTENT_MULTI_GROUP  = "multi_group"  # multiple products → ONE group image
_INTENT_CATALOGUE    = "catalogue"
_INTENT_BRAND        = "brand"
_INTENT_AMBIGUOUS    = "ambiguous"

# Keywords that signal the user wants SEPARATE images per product
_INDIVIDUAL_KWS = [
    "individually", "each one", "each product", "separate", "one per",
    "one for each", "one each", "per product", "individual image",
    "individual promo", "separate image",
]

# Products with multiple label versions — ask user to choose before generating
# Maps base product name → (v1 name, v2 name)
_LABEL_VERSION_PRODUCTS = {
    "Blood Circulations": ("Blood Circulations", "Herbal Vitality & Flow Support 2"),
    "Blood Purifying":    ("Blood Purifying",    "Herbal Vitality Blend 2"),
}

# Display names used in prompts — strips internal version suffixes so AI doesn't render "2" on labels
_PROMPT_DISPLAY_NAMES = {
    "Herbal Vitality & Flow Support 2": "Herbal Vitality & Flow Support",
    "Herbal Vitality Blend 2":          "Herbal Vitality Blend",
}

def _prompt_name(name: str) -> str:
    """Return the display name to use inside image/video prompts (strips internal version suffixes)."""
    return _PROMPT_DISPLAY_NAMES.get(name, name)

# Products that are smaller-format containers (green lid = small jar, roughly half the height of capsule bottles)
_SMALL_FORMAT_PRODUCTS = {"Detox"}
_SMALL_FORMAT_KEYWORDS = {"detox"}  # lower-case keywords for detection

def _build_group_sizing_note(products: list) -> str:
    """
    Returns a sizing instruction when the group contains a mix of small and standard products.
    Detects small-format products (green lid, small jar) and calls out the relative size difference.
    """
    small = [_prompt_name(p["name"]) for p in products
             if p["name"] in _SMALL_FORMAT_PRODUCTS
             or any(kw in p["name"].lower() for kw in _SMALL_FORMAT_KEYWORDS)]
    standard = [_prompt_name(p["name"]) for p in products
                if p["name"] not in _SMALL_FORMAT_PRODUCTS
                and not any(kw in p["name"].lower() for kw in _SMALL_FORMAT_KEYWORDS)]
    if not small or not standard:
        return ""
    small_str = ", ".join(small)
    standard_str = ", ".join(standard)
    return (
        f"PRODUCT SIZING — CRITICAL: The products in this group are NOT all the same size. "
        f"{small_str} is a SMALL FORMAT jar (green lid) — it is approximately HALF the height "
        f"and half the width of the standard capsule bottles ({standard_str}). "
        f"Render {small_str} visibly and noticeably smaller — it should look like a compact jar "
        f"sitting in front of or beside the taller bottles, never the same height. "
        f"Do NOT scale all products to the same height.\n\n"
    )

# Positive feedback keywords
_FEEDBACK_POSITIVE = {
    "yes", "happy", "looks good", "perfect", "great", "love it",
    "good", "nice", "approved", "approve", "love", "beautiful", "excellent",
}


def classify_intent(msg_text: str, cat: list) -> dict:
    """
    Classify the intent of a chat message.
    Returns {"intent": str, "products": list}
    """
    t = msg_text.lower().strip()

    # Explicit catalogue listing request
    catalogue_kws = [
        "list all", "show all", "all products", "full catalogue",
        "full catalog", "what products do you have", "show me everything",
    ]
    if any(kw in t for kw in catalogue_kws):
        return {"intent": _INTENT_CATALOGUE, "products": []}

    # Brand / colour question
    brand_kws = [
        "brand colour", "brand color", "colour palette", "color palette",
        "brand info", "about dr gee", "brand detail", "label colour", "label color",
    ]
    if any(kw in t for kw in brand_kws):
        return {"intent": _INTENT_BRAND, "products": []}

    # Find all exact product name matches
    matched = find_all_products(t, cat)
    if len(matched) == 1:
        return {"intent": _INTENT_SINGLE, "products": matched}
    if len(matched) > 1:
        # Default: group shot (one image with all products)
        # Only split into separate images if explicitly requested
        wants_individual = any(kw in t for kw in _INDIVIDUAL_KWS)
        intent = _INTENT_MULTI if wants_individual else _INTENT_MULTI_GROUP
        return {"intent": intent, "products": matched}

    # Try fuzzy single match
    fuzzy = find_product(t, cat)
    if fuzzy:
        return {"intent": _INTENT_SINGLE, "products": [fuzzy]}

    return {"intent": _INTENT_AMBIGUOUS, "products": []}


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------
DEFAULT_STYLE = (
    "Premium South African herbal wellness brand aesthetic. Deep teal-to-charcoal "
    "gradient background transitioning from #1a3a3a to #2d2d2d. Warm diffused studio "
    "lighting from upper-left creating soft natural shadows. Clean, modern, trustworthy "
    "atmosphere. Professional health and wellness mood — natural and authoritative, not "
    "clinical. Subtle circular decorative elements and organic shapes in the background "
    "at low opacity."
)

DEFAULT_COMPOSITION = (
    "3:4 portrait aspect ratio composition. Product positioned in the lower two-thirds "
    "occupying approximately 60% of image height — vary between centered and a slight "
    "rule-of-thirds offset (shifted left or right of center by 10-15%) for a natural "
    "lifestyle feel rather than always dead-center. Generous negative space in the upper "
    "third reserved for post-production text overlay. Straight-on front-facing shot "
    "with subtle 5-degree tilt. Product on a barely-visible reflective surface with "
    "soft mirror effect."
)

TECHNICAL_SPECS = (
    "3:4 portrait aspect ratio, 1080x1440 pixels. High resolution, professional product "
    "photography quality. Sharp focus on product with smooth background. Commercial "
    "advertising standard suitable for Instagram, Facebook, and WhatsApp marketing. "
    "No watermarks. No stock photo artifacts. Photorealistic rendering."
)


def assemble_prompt(product, cache_entry=None, extras=None, scene_desc=None, img_file=None, scene_brief=None):
    """
    scene_brief: parsed dict from analyze_scene_image() — structured scene analysis.
                 When present, produces a far more precise Layer 1 than plain scene_desc text.
    scene_desc:  fallback plain-text description when no brief is available.
    """
    name = _prompt_name(product["name"])   # strip internal version suffixes (e.g. "2") from prompt text
    price = product.get("price", 0)
    description = product.get("description", "")

    has_scene = bool(scene_brief or scene_desc)

    # Layer 1 — Style
    if scene_brief:
        # Rich structured analysis from the scene analyser agent
        keep  = scene_brief.get("keep", "")
        change = scene_brief.get("change", "")
        scene_prompt = scene_brief.get("scene_prompt", "")
        subjects = scene_brief.get("subjects", "")
        lighting = scene_brief.get("lighting", "")
        surfaces = scene_brief.get("surfaces", "")
        atmosphere = scene_brief.get("atmosphere", "")

        keep_clause = f" PRESERVE EXACTLY: {keep}." if keep else ""
        change_clause = f" CHANGE ONLY: {change}." if change else ""
        subjects_clause = (
            f" SUBJECTS: {subjects} — reproduce their exact position, gesture and appearance;"
            f" do not remove or alter them unless listed in CHANGE ONLY."
        ) if subjects and subjects.lower() not in ("none", "no people", "") else ""

        layer1 = (
            "IMAGE 1 (FIRST ATTACHED IMAGE) IS THE SCENE/STYLE REFERENCE — a real photograph.\n\n"
            f"SCENE ENVIRONMENT: {scene_prompt}\n\n"
            f"LIGHTING: {lighting}. SURFACES: {surfaces}. ATMOSPHERE: {atmosphere}.\n\n"
            f"{keep_clause}{subjects_clause}{change_clause}\n\n"
            "Replicate every environmental detail of this reference photograph with photographic accuracy. "
            "Do NOT invent a new background — rebuild this exact scene."
        )
    elif scene_desc:
        # Fallback: plain text description (no vision analysis available)
        layer1 = (
            "IMAGE 1 (FIRST ATTACHED IMAGE) IS THE SCENE/STYLE REFERENCE. "
            "This is a real photograph provided as the visual environment for this image. "
            "You must replicate its background, surfaces, lighting direction, colour temperature, "
            "atmosphere, props, textures, and mood EXACTLY. "
            "Do NOT copy any products, people, text, or branding from this reference photo — "
            "only use its environment and lighting as the scene. "
            "The Dr Gee product(s) must be placed INTO this scene as if physically present, "
            "picking up the scene's lighting, reflections, and shadows naturally."
        )
    else:
        layer1 = DEFAULT_STYLE

    # Build product image preamble — changes wording depending on whether a scene image is also attached
    if has_scene and img_file:
        _product_ref_label = (
            "IMAGE 2 ONWARDS (REMAINING ATTACHED IMAGES) ARE THE PRODUCT REFERENCES. "
            "These show the exact Dr Gee product(s) that must appear in this image. "
        )
    elif img_file:
        _product_ref_label = (
            "The attached image(s) are the exact visual reference for the product(s) "
            "that must appear in this image. "
        )
    else:
        _product_ref_label = ""

    _image_preamble = (
        _product_ref_label
        + "These attached product photos are the absolute ground truth for the shape, "
        "packaging design, label design, label text, branding, colors, cap style, and "
        "all physical details of the products. Reproduce every packaging detail "
        "IDENTICALLY — do not alter, redesign, reimagine, stylize, or change anything "
        "about the product itself.\n\n"
        "ORIGINAL BACKGROUND REMOVAL — CRITICAL: The product image was photographed "
        "against a plain studio backdrop (white, grey, or neutral). That studio "
        "background must be COMPLETELY REMOVED and replaced with the new scene "
        "environment described above. Do not retain any of the product's original "
        "backdrop — only keep the physical product itself, cut out cleanly, and "
        "composite it into the new scene.\n\n"
        "SEAMLESS SCENE INTEGRATION: The product photos were taken against a studio "
        "background and will have studio-specific lighting artifacts — rim lights, "
        "white specular highlights, colored background reflections — that must be "
        "completely removed and replaced with scene-appropriate lighting. This is a "
        "full environment relighting, not just a background swap.\n\n"
        "NO ADDITIONS TO LABELS OR PACKAGING — CRITICAL: Do NOT add any illustrations, "
        "icons, leaf motifs, herb graphics, decorative symbols, ornaments, or any "
        "embellishment to the product labels or packaging that are not physically "
        "visible in the attached reference product images. Do NOT render any version "
        "numbers, numerals, or suffixes on labels that do not appear in the reference "
        "photo. The label and packaging must be reproduced with absolute fidelity — "
        "nothing added, nothing removed, nothing re-styled.\n\n"
        "SPECULAR HIGHLIGHTS AND REFLECTIONS — CRITICAL: Any bright white or "
        "studio-colored specular highlight currently visible on the product must be "
        "eliminated. Replace all specular highlights with colors from the actual scene "
        "environment. The product's surfaces should act as mirrors reflecting the "
        "dominant colors of the surrounding scene — its specular, shadows, and ambient "
        "light must all match the scene's lighting setup as if the product was "
        "physically photographed there.\n\n"
        "SHADOW AND AMBIENT OCCLUSION: Cast soft natural shadows in the direction of "
        "the scene's light source. Add contact shadow where the product meets any "
        "surface. The scene's ambient light should wrap softly around the product.\n\n"
        "SCENE COLOR TEMPERATURE — CRITICAL: The color of specular highlights, ambient "
        "wrap light, and surface reflections on ALL product surfaces must match the "
        "scene's color temperature EXACTLY. If the scene is warm (golden hour, rustic "
        "wood, candlelight, afternoon sun) then ALL highlights on white plastic, glass, "
        "and caps must be warm amber/golden — NEVER cool-white or blue. If the scene is "
        "cool or neutral, match that instead. Applying blue or cool-toned studio "
        "highlights to products in a warm scene is a critical compositing error — it "
        "makes the product look pasted-in and must be completely avoided. Every surface "
        "that catches light must reflect the scene's dominant warm or cool hue.\n\n"
        "AMBER GLASS BOTTLE TRANSLUCENCY — CRITICAL: Amber/dark glass bottles "
        "(dropper bottles, tincture bottles, spray bottles with amber glass bodies) "
        "are SEMI-TRANSPARENT — NOT solid opaque plastic. Light passes through amber "
        "glass with a warm honey-brown tint. Scene elements behind the bottle must be "
        "partially visible through the glass body, filtered through the amber color. "
        "Bright areas behind the bottle glow warmly as rich honey-amber through the "
        "glass. Darker areas show as deep amber-brown. The glass body must have "
        "visible depth and translucency — never render it as a solid painted surface. "
        "Applies to: Eye and Ear Drops, tincture bottles, QS7 Syrup, QS8 sprays. "
        "Does NOT apply to dark opaque PLASTIC Q Lyfe bottles (Herbal Boost Blend, "
        "Libido Tonic, Corrective for Women) — those are solid with no light "
        "transmission.\n\n"
        "SILVER/METALLIC MYLAR POUCH PACKAGING — SPECIAL REFLECTION RULES: If the "
        "product is a stand-up ziplock pouch, its body is FULLY SILVER/CHROME METALLIC "
        "mylar — header, gussets, and back panel are all solid silver/chrome. Any blue "
        "or dark tinting at the pouch edges in the reference image is a background-"
        "removal editing artifact — ignore it. The actual pouch is entirely silver/"
        "metallic. Silver metallic mylar behaves like a chrome mirror: it reflects the "
        "scene's dominant colors crisply and strongly. A blue abstract scene = bold "
        "blue streaks on the silver sides. A sky scene = sky blue and cloud white "
        "mirrored on the chrome. A warm rustic scene = amber/golden mirror reflections. "
        "Reflections on silver are more vivid and sharp than on dark glass — show clear "
        "scene-color gradients on the metallic surfaces. The transparent front window "
        "shows the scene environment through it with a soft interior product glow.\n\n"
    )

    # Layer 2 — Product
    if cache_entry:
        # Full cache: attached image instruction + confirmed visual description
        visual_desc = cache_entry.get("visual_description", "")
        layer2 = (
            _image_preamble
            + f"REINFORCEMENT DESCRIPTION: {visual_desc}\n\n"
            "IMPORTANT: The attached product images are the definitive reference for "
            "all packaging details. The only things that should adapt to the new scene "
            "are: lighting on the product exterior, specular highlight colors (must "
            "match the scene environment — no studio-white highlights), environment-"
            "matched reflections on the product's surfaces, and shadows. Everything "
            "printed on or part of the physical product stays exactly as shown. Studio "
            "lighting artifacts from the original product photo must be fully removed "
            "and replaced — they must not appear in the final image."
        )
    elif img_file:
        # Image exists but not yet analyzed — attach it and use catalogue description
        layer2 = (
            _image_preamble
            + f"REINFORCEMENT DESCRIPTION: {name} by Dr Gee — {description}\n\n"
            "IMPORTANT: The attached product images are the definitive reference for "
            "all packaging details. The only things that should adapt to the new scene "
            "are: lighting on the product exterior, specular highlight colors (must "
            "match the scene environment — no studio-white highlights), environment-"
            "matched reflections on the product's surfaces, and shadows. Everything "
            "printed on or part of the physical product stays exactly as shown. Studio "
            "lighting artifacts from the original product photo must be fully removed "
            "and replaced — they must not appear in the final image."
        )
    else:
        # No image at all — text only
        layer2 = (
            f"The product is {name} by Dr Gee — a premium South African herbal "
            f"wellness product. {description}"
        )

    layer3 = DEFAULT_COMPOSITION
    layer4 = (
        "TEXT RULE: Generate this image with NO text, words, characters, numbers, or "
        "typography of any kind — except the text that is physically printed on the "
        "product labels and packaging in the attached reference images. Do not add "
        "product names, prices, taglines, sale badges, brand slogans, social handles, "
        "website URLs, or any other copy anywhere in the image. All promotional text "
        "will be applied as post-production overlays in Canva. The image must be a "
        "clean, text-free visual suitable for text to be layered on top."
    )
    layer5 = extras if extras else ""
    layer6 = TECHNICAL_SPECS

    layers = [layer1, layer2, layer3, layer4]
    if layer5:
        layers.append(layer5)
    layers.append(layer6)
    return "\n\n".join(layers)


def assemble_video_prompt(product, cache_entry=None, scene_desc="premium wellness setting", img_file=None, scene_brief=None):
    name = _prompt_name(product["name"])

    # Build per-product locked elements from cache if available
    if cache_entry:
        visual = cache_entry.get("visual_description", "")
        cap_hint = ""
        if "white" in visual.lower() and "cap" in visual.lower():
            cap_hint = "WHITE smooth screw cap"
        elif "green" in visual.lower() and "cap" in visual.lower():
            cap_hint = "FOREST GREEN smooth screw cap"
        elif "black" in visual.lower() and "cap" in visual.lower():
            cap_hint = "BLACK ribbed plastic screw cap"
        else:
            cap_hint = "cap as shown in source image"
        locked_product = (
            f"- {name}: {cap_hint}. Label reads exactly as printed on the physical "
            f"packaging in the source image. All label text, logo, ornamental scrollwork, "
            f"and packaging details must be reproduced exactly."
        )
    elif img_file:
        # Image attached but not yet analyzed — reference the attached image
        locked_product = (
            f"- {name}: all cap color, label text, logo, ornamental scrollwork, and "
            f"packaging details must match the attached source image exactly."
        )
    else:
        locked_product = (
            f"- {name}: all cap color, label text, logo, ornamental scrollwork, and "
            f"packaging details must match the Dr Gee brand standard exactly."
        )

    return (
        f"The attached image is the ABSOLUTE GROUND TRUTH for this video. This is not "
        f"a style reference — it is a locked frame. Every element in it must be "
        f"reproduced identically in every frame of the video. The source image contains "
        f"NO text overlays — do not add any.\n\n"
        f"LOCKED ELEMENTS — must not change in any frame:\n\n"
        f"PRODUCTS:\n{locked_product}\n"
        f"All bottle shapes, sizes, label backgrounds, logo ornaments, cap colors, and "
        f"packaging details must match the source image exactly. No alterations to any "
        f"product packaging whatsoever.\n\n"
        f"TEXT RULE: The source image contains NO text overlays — only the text "
        f"physically printed on the product labels. Do NOT add, generate, render, or "
        f"invent any text, words, characters, numbers, captions, subtitles, watermarks, "
        f"or typography anywhere in this video. Not at the start, not at the end, not "
        f"as a lower-third, not as a title card, not at the bottom of frame. Zero "
        f"invented text. The ONLY readable text permitted is what is physically printed "
        f"on the product labels.\n\n"
        f"SCENE: {name} product on a premium surface in a {scene_brief.get('scene_prompt', scene_desc) if scene_brief else scene_desc}. Scene "
        f"composition and all props remain unchanged throughout.\n\n"
        f"The FIRST FRAME must be an exact photographic match of the attached source "
        f"image. Every subsequent frame maintains the same product appearance and same "
        f"scene layout.\n\n"
        f"ANIMATION: A Black South African woman, 28-35, natural hair, warm neutral "
        f"clothing, enters softly from the RIGHT edge of the frame. She is ALWAYS in "
        f"heavy bokeh blur — never sharp, never in focus at any point in the video. "
        f"She occupies no more than 15% of the frame width and remains at the far right "
        f"edge only, never moving behind or in front of the product. She slowly reaches "
        f"in from the right edge, briefly touches the product, then gently withdraws. "
        f"Her movement is slow, graceful, and peripheral. She must never obscure any "
        f"product, any label, or any packaging detail.\n\n"
        f"The camera performs an extremely slow push-in — no more than 5% closer over "
        f"the full duration. No cuts. No camera shake. Lighting holds constant. No "
        f"colour grade changes — match the tone of the source image exactly.\n\n"
        f"Duration: 7 seconds. Aspect ratio: 3:4 portrait. Photorealistic. Zero text "
        f"overlays of any kind."
    )


import random as _random

def assemble_group_video_prompt(products: list, scene_brief: dict | None = None, scene_desc: str = "premium wellness setting") -> str:
    """Build a video prompt for a group shot — subtle camera move + optional blurred background person."""
    names_str = ", ".join(_prompt_name(p["name"]) for p in products)
    scene_context = (scene_brief.get("scene_prompt", scene_desc) if scene_brief else scene_desc)
    lighting = (scene_brief.get("lighting", "") if scene_brief else "")
    atmosphere = (scene_brief.get("atmosphere", "") if scene_brief else "")

    # Randomly vary the background person interaction (or omit entirely ~25% of the time)
    _interactions = [
        (
            "A Black South African woman, 28-35, natural hair, warm neutral clothing, "
            "walks slowly through the background from left to right — or right to left — "
            "at the far rear of the scene. She is ALWAYS in heavy bokeh blur, never sharp. "
            "She pauses briefly mid-frame, glances softly toward the products, then continues out of frame. "
            "Her movement is slow and graceful."
        ),
        (
            "A Black South African woman, 28-35, natural hair, warm neutral clothing, "
            "is partially visible in the background — she reaches toward an object on a shelf or counter "
            "behind the products, picks it up gently, and replaces it. "
            "She is ALWAYS in heavy bokeh blur, never in focus. "
            "She stays fully behind the products and never obscures any label."
        ),
        (
            "A Black South African man, 30-40, clean casual attire, "
            "briefly enters the background from the right, pours or sets down a glass, "
            "then moves off-frame again. "
            "He is ALWAYS in heavy bokeh blur — never sharp. "
            "He remains well behind the products at all times."
        ),
        None,  # no person — pure scene animation
    ]
    person_action = _random.choice(_interactions)

    person_block = (
        f"BACKGROUND PERSON: {person_action}\n"
        f"The person must NEVER be in focus at any point — maintain consistent heavy blur throughout. "
        f"They must never overlap or obscure any product, any label, or any packaging detail. "
        f"They occupy no more than 20% of the frame and remain in the background only.\n\n"
        if person_action else
        "BACKGROUND: No person — the scene animates naturally with the camera movement only.\n\n"
    )

    # Subtle camera movement
    _camera_moves = [
        "An extremely slow push-in — no more than 5% closer over the full 7 seconds.",
        "An extremely slow pull-back — starting slightly closer, easing back no more than 5% over 7 seconds.",
        "A barely perceptible slow pan left-to-right across the product group — no more than 3% horizontal drift.",
    ]
    camera_move = _random.choice(_camera_moves)

    locked_products = "\n".join(
        f"- {_prompt_name(p['name'])}: all label text, logo, ornamental scrollwork, cap color, and packaging details must match the attached source images exactly."
        for p in products
    )

    return (
        f"The attached image is the ABSOLUTE GROUND TRUTH for this video. Every element in it must be "
        f"reproduced identically in every frame. The source image contains NO text overlays — do not add any.\n\n"
        f"LOCKED ELEMENTS — must not change in any frame:\n\n"
        f"PRODUCTS:\n{locked_products}\n"
        f"All bottle shapes, sizes, label backgrounds, logo ornaments, cap colors, and packaging details must "
        f"match the source image exactly. NO additions to labels — no illustrations, icons, leaf graphics, "
        f"numerals, or decorative elements not present in the reference images.\n\n"
        f"TEXT RULE: Do NOT add, generate, render, or invent any text, words, numbers, captions, subtitles, "
        f"watermarks, or typography anywhere in this video. The ONLY readable text is what is physically printed "
        f"on the product labels.\n\n"
        f"SCENE: {len(products)} Dr Gee products grouped together in — {scene_context}. "
        f"{'Lighting: ' + lighting + '. ' if lighting else ''}"
        f"{'Atmosphere: ' + atmosphere + '. ' if atmosphere else ''}"
        f"Scene composition, surface, props, and all background elements remain unchanged throughout.\n\n"
        f"The FIRST FRAME must be an exact photographic match of the attached source image.\n\n"
        f"{person_block}"
        f"CAMERA: {camera_move} No cuts. No camera shake. Lighting holds constant. "
        f"Colour grade matches the source image tone exactly.\n\n"
        f"Duration: 7 seconds. Aspect ratio: 3:4 portrait. Photorealistic. Zero text overlays of any kind."
    )


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# SSE job progress tracking
# ---------------------------------------------------------------------------
_jobs: dict = {}
_jobs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Session state — per-sender context for feedback loop & clarification
# ---------------------------------------------------------------------------
_session_state: dict = {}
_session_lock = threading.Lock()

def _get_session(sender: str) -> dict:
    with _session_lock:
        return dict(_session_state.get(sender, {}))

def _set_session(sender: str, updates: dict):
    with _session_lock:
        _session_state.setdefault(sender, {}).update(updates)

def _clear_session(sender: str, *keys):
    with _session_lock:
        s = _session_state.get(sender, {})
        for k in keys:
            s.pop(k, None)

def _new_job():
    """Create a new job queue. Returns (job_id, queue)."""
    jid = uuid.uuid4().hex[:10]
    q = _queue_mod.Queue()
    with _jobs_lock:
        _jobs[jid] = q
    return jid, q

def _emit(jid: str, msg_type: str, message: str, payload: dict | None = None):
    """Push a progress event to the job queue."""
    with _jobs_lock:
        q = _jobs.get(jid)
    if q:
        q.put({"type": msg_type, "message": message, **(payload or {})})

@app.route("/api/stream/<jid>")
def stream_job(jid):
    """SSE endpoint — streams job progress to the frontend."""
    def generate():
        with _jobs_lock:
            q = _jobs.get(jid)
        if not q:
            yield f"data: {json.dumps({'type':'error','message':'Job not found'})}\n\n"
            return
        try:
            while True:
                try:
                    item = q.get(timeout=300)
                    yield f"data: {json.dumps(item)}\n\n"
                    if item.get("type") in ("done", "error"):
                        break
                except _queue_mod.Empty:
                    yield "data: {\"type\":\"heartbeat\"}\n\n"
                    break
        finally:
            with _jobs_lock:
                _jobs.pop(jid, None)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/poll/<jid>")
def poll_job(jid):
    """Polling endpoint — returns all pending events for a job as JSON array.
    Used as fallback when EventSource is blocked (HTTPS page → HTTP server).
    """
    with _jobs_lock:
        q = _jobs.get(jid)
    if not q:
        return jsonify({"events": [{"type": "error", "message": "Job not found or expired"}]})
    events = []
    try:
        while True:
            item = q.get_nowait()
            events.append(item)
            if item.get("type") in ("done", "error"):
                with _jobs_lock:
                    _jobs.pop(jid, None)
                break
    except _queue_mod.Empty:
        pass
    return jsonify({"events": events})


# ---------------------------------------------------------------------------
# Showcase generation helpers
# ---------------------------------------------------------------------------

def _write_manifest():
    """Scan GENERATED_DIR for PNGs, build/write generated/manifest.json keeping newest per slug."""
    images = {}
    if GENERATED_DIR.exists():
        for f in sorted(GENERATED_DIR.glob("*.png"), key=lambda x: x.stat().st_mtime):
            if "_social_" in f.stem:
                slug = f.stem.split("_social_")[0]
                images[slug] = f.name  # later mtime wins
    manifest = {"generated_at": datetime.now().isoformat() + "Z", "images": images}
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with open(GENERATED_DIR / "manifest.json", "w", encoding="utf-8") as mf:
        json.dump(manifest, mf, indent=2)
    print(f"  [Manifest] Written: {len(images)} products")
    return manifest


def _git_push_generated(jid, commit_msg):
    """Git add generated/, commit, push origin main. Returns True on success."""
    repo = Path(__file__).parent
    try:
        _emit(jid, "step", "Committing images to git...")
        r = subprocess.run(["git", "add", "generated/"], cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            _emit(jid, "step", f"git add warning: {r.stderr[:80]}")

        r = subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo, capture_output=True, text=True)
        combined = (r.stdout + r.stderr).lower()
        if r.returncode != 0:
            if "nothing to commit" in combined:
                _emit(jid, "step", "Nothing new to commit")
                return True
            _emit(jid, "step", f"git commit failed: {r.stderr[:80]}")
            return False

        _emit(jid, "step", "Committed. Pushing to GitHub...")
        r = subprocess.run(["git", "push", "origin", "main"], cwd=repo, capture_output=True, text=True)
        if r.returncode != 0:
            _emit(jid, "step", f"Push failed: {r.stderr[:80]}")
            return False
        _emit(jid, "step", "Pushed to GitHub Pages")
        return True
    except Exception as e:
        _emit(jid, "step", f"Git error: {str(e)[:80]}")
        return False


def _run_showcase_gen_job(jid, slugs_to_generate):
    """Background thread — generates showcase images for the given slugs, then commits + pushes."""
    try:
        from generate_social_image import generate_social_image
    except ImportError as e:
        _emit(jid, "error", f"Cannot import generate_social_image: {e}")
        return

    total = len(slugs_to_generate)
    generated = 0

    for i, slug in enumerate(slugs_to_generate):
        # Find product in catalogue by slug
        product = None
        for p in catalogue:
            p_slug = re.sub(r"[^a-z0-9]+", "-", p["name"].lower()).strip("-")
            if p_slug == slug:
                product = p
                break
        if not product:
            _emit(jid, "step", f"[{i+1}/{total}] Skipping {slug} — not in catalogue")
            continue

        name = product["name"]
        _emit(jid, "step", f"[{i+1}/{total}] Generating {name}...")

        # Find cache entry
        cache_entry = None
        for cs, ce in cache.items():
            if ce.get("product_name", "").lower() == name.lower() or cs == slug:
                cache_entry = ce
                break

        img_file = find_image_for_product(name)

        # Delete old images for this slug to avoid duplicates in git
        if GENERATED_DIR.exists():
            for old in GENERATED_DIR.glob(f"{slug}_social_*.png"):
                try:
                    old.unlink()
                except Exception:
                    pass

        image_prompt = assemble_prompt(product, cache_entry, None, None, img_file=img_file, scene_brief=None)

        try:
            result = generate_social_image(
                prompt=image_prompt,
                product_slug=slug,
                output_dir=str(GENERATED_DIR),
                product_image_files=[img_file] if img_file else None,
                on_progress=lambda msg: _emit(jid, "step", msg),
            )
            if result.get("local_path"):
                generated += 1
                _emit(jid, "step", f"[{i+1}/{total}] {name} done")
            else:
                _emit(jid, "step", f"[{i+1}/{total}] {name} — generation may have failed")
        except Exception as e:
            _emit(jid, "step", f"[{i+1}/{total}] {name} error: {str(e)[:60]}")

    _emit(jid, "step", "Writing manifest.json...")
    _write_manifest()
    _git_push_generated(jid, f"chore: update showcase images ({generated}/{total} generated)")
    _emit(jid, "done", "Showcase updated", {"refreshed": generated})


catalogue = load_catalogue()
cache = load_cache()

# Build enriched catalogue with image info
def build_enriched_catalogue():
    enriched = []
    for p in catalogue:
        name = p["name"]
        img_file = find_image_for_product(name)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        cache_entry = None
        for cs, ce in cache.items():
            if ce.get("product_name", "").lower() == name.lower():
                cache_entry = ce
                break
        enriched.append({
            "name": name,
            "product_line": p.get("product_line", ""),
            "price": p.get("price", 0),
            "description": p.get("description", ""),
            "sku": p.get("sku", ""),
            "variant": p.get("variant", ""),
            "components": p.get("components", ""),
            "slug": slug,
            "old_name": OLD_NAMES.get(name, p.get("product_line", "")),
            "image_file": img_file,
            "image_url": f"/api/product-image/{quote(img_file)}" if img_file else None,
            "cached": cache_entry is not None,
            "has_visual_desc": bool(cache_entry),
        })
    return enriched

enriched_catalogue = build_enriched_catalogue()

_debug_msg = 'please replace the products in the reference image with Herbal Vitality & Flow Support, Herbal Vitality Blend, detox and advanced liver kidney and bladder syrup without changing anything else'
_debug_found = find_all_products(_debug_msg, catalogue)
print(f"\n[CWS DEBUG] find_all_products test: {[p['name'] for p in _debug_found]}")
print(f"\n[CWS Server] Loaded {len(catalogue)} products from catalogue")
print(f"[CWS Server] Loaded {len(cache)} cached product descriptions")
print(f"[CWS Server] {sum(1 for p in enriched_catalogue if p['image_file'])} products have images")


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "CWS Simulator Workspace — Local Server",
        "status": "running",
        "products_loaded": len(catalogue),
        "products_with_images": sum(1 for p in enriched_catalogue if p["image_file"]),
        "cached_products": len(cache),
    })


@app.route("/api/catalogue", methods=["GET"])
def api_catalogue():
    """Full product catalogue with image URLs and metadata."""
    return jsonify(enriched_catalogue)


@app.route("/api/product-image/<path:filename>", methods=["GET"])
def product_image(filename):
    """Serve product images."""
    return send_from_directory(str(PRODUCTS_DIR), filename)


@app.route("/api/generated/<path:filename>", methods=["GET"])
def generated_image(filename):
    """Serve generated images."""
    return send_from_directory(str(GENERATED_DIR), filename)


@app.route("/webhook", methods=["POST"])
def webhook():
    """Main chat webhook — returns job_id immediately, processes in background."""
    try:
        data = request.get_json(force=True)
        msg_text = data.get("message", {}).get("text", "").strip()
        sender = data.get("sender", {}).get("name", "User")
        selected_product = data.get("selected_product", None)

        # --- Greeting ---
        greetings = ["hi", "hello", "hey", "howzit", "good morning", "good afternoon"]
        if msg_text.lower().strip() in greetings:
            return jsonify({
                "reply": (
                    f"Hello {sender}! Welcome to the Dr Gee promo image generator.\n\n"
                    f"You can:\n"
                    f"  1. Type a product name (e.g., 'Blood Circulation promo')\n"
                    f"  2. Use the catalogue button to browse and select products\n"
                    f"  3. Upload a scene image for custom styling\n\n"
                    f"I'll generate a 3:4 portrait promo image and video prompt for you."
                )
            })

        # --- Label version reply (e.g. "1", "old", "2", "new") ---
        session = _get_session(sender)
        if session.get("pending_version_product") and msg_text:
            t_reply = msg_text.lower().strip()
            base = session["pending_version_product"]
            orig_data = session["pending_version_data"]
            v1_name, v2_name = _LABEL_VERSION_PRODUCTS[base]
            chosen = None
            if t_reply in ("1", "old", "v1", "old label", "first"):
                chosen = v1_name
            elif t_reply in ("2", "new", "v2", "new label", "second"):
                chosen = v2_name
            if chosen:
                _clear_session(sender, "pending_version_product", "pending_version_data")
                # Swap the product name in the original request data so _process_request uses the right one
                orig_data = dict(orig_data)
                orig_data["_override_product_name"] = chosen
                jid, _ = _new_job()
                threading.Thread(target=_process_request, args=(jid, orig_data), daemon=True).start()
                return jsonify({"job_id": jid})
            else:
                return jsonify({"reply": "Please reply *1* (old label) or *2* (new label)."})

        # --- Feedback loop: awaiting a response after a generation ---
        if session.get("awaiting_feedback") and msg_text:
            jid, _ = _new_job()
            threading.Thread(
                target=_handle_feedback_job,
                args=(jid, sender, msg_text),
                daemon=True,
            ).start()
            return jsonify({"job_id": jid})

        # --- Selected product(s) from catalogue browser (skip text classification) ---
        selected_products = data.get("selected_products") or []
        if selected_product and not selected_products:
            selected_products = [selected_product]

        if len(selected_products) > 1:
            # Catalogue browser selection — check if message text requests individual images
            sel_text = (data.get("message", {}).get("text", "") or "").lower()
            wants_individual = any(kw in sel_text for kw in _INDIVIDUAL_KWS)
            jid, _ = _new_job()
            if wants_individual:
                threading.Thread(
                    target=_run_multi_product_chat_job,
                    args=(jid, data, selected_products),
                    daemon=True,
                ).start()
                return jsonify({"job_id": jid, "multi": True, "count": len(selected_products)})
            else:
                threading.Thread(
                    target=_run_group_chat_job,
                    args=(jid, data, selected_products),
                    daemon=True,
                ).start()
                return jsonify({"job_id": jid, "multi": False, "group": True, "count": len(selected_products)})

        if selected_products:
            jid, _ = _new_job()
            threading.Thread(target=_process_request, args=(jid, data), daemon=True).start()
            return jsonify({"job_id": jid})

        if not msg_text:
            return jsonify({"reply": "Please tell me which product you'd like, or use the catalogue to select one."})

        # If user uploaded a reference scene image, skip catalogue/brand intents —
        # the message is always a generation request with the scene context.
        has_scene = bool(data.get("scene_description") or data.get("message", {}).get("image"))

        # --- Intent classification ---
        intent_result = classify_intent(msg_text, catalogue)
        intent  = intent_result["intent"]
        matched = intent_result["products"]
        print(f"[webhook] intent={intent} has_scene={has_scene} matched={[p['name'] for p in matched]} msg={msg_text[:80]!r}", flush=True)

        if intent == _INTENT_CATALOGUE and not has_scene:
            product_list = "\n".join([f"  - {p['name']} (R{p.get('price', '?')})" for p in catalogue])
            return jsonify({"reply": f"Dr Gee Product Catalogue:\n\n{product_list}"})

        if intent == _INTENT_BRAND:
            return jsonify({"reply": (
                "Dr Gee brand details:\n\n"
                "Label style: Dark charcoal/black background, gold ornamental scrollwork, cream/gold typography.\n"
                "Colours: Charcoal black (#1a1a1a), Gold (#c9a84c), Cream (#f5f0e8)\n"
                "Brand name on labels: DR GEE (uppercase, no period, gold serif font)\n"
                "Aesthetic: Premium herbal wellness — dark, gold, authoritative yet natural."
            )})

        if intent == _INTENT_MULTI_GROUP:
            # One image with all products grouped together
            jid, _ = _new_job()
            threading.Thread(
                target=_run_group_chat_job,
                args=(jid, data, [p["name"] for p in matched]),
                daemon=True,
            ).start()
            return jsonify({"job_id": jid, "multi": False, "group": True, "count": len(matched)})

        if intent == _INTENT_MULTI:
            # Separate image per product (explicit request)
            jid, _ = _new_job()
            threading.Thread(
                target=_run_multi_product_chat_job,
                args=(jid, data, [p["name"] for p in matched]),
                daemon=True,
            ).start()
            return jsonify({"job_id": jid, "multi": True, "count": len(matched)})

        if intent == _INTENT_AMBIGUOUS and not has_scene:
            return jsonify({"reply": (
                "I'm not sure which product you mean. Could you be more specific?\n\n"
                "Try typing the full product name, or tap the catalogue button to browse all 45 products."
            )})

        # intent == _INTENT_SINGLE — check for label version ambiguity before generating
        if matched and matched[0]["name"] in _LABEL_VERSION_PRODUCTS and not has_scene:
            base = matched[0]["name"]
            v1_name, v2_name = _LABEL_VERSION_PRODUCTS[base]
            _set_session(sender, {"pending_version_product": base, "pending_version_data": data})
            return jsonify({"reply": (
                f"*{base}* has two label versions — which would you like?\n\n"
                f"  1️⃣  *Old label* — \"{v1_name.replace('Blood Circulations','Blood Circulation')}\"\n"
                f"  2️⃣  *New label* — \"{v2_name}\"\n\n"
                "Reply *1* or *old*, or *2* or *new*."
            )})

        jid, _ = _new_job()
        threading.Thread(target=_process_request, args=(jid, data), daemon=True).start()
        return jsonify({"job_id": jid})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"reply": f"Server error: {str(e)}"}), 500


def _run_group_chat_job(jid: str, data: dict, product_names: list):
    """Background thread — generates ONE image with ALL listed products arranged together in the scene."""
    def progress(msg):
        _emit(jid, "step", msg)

    try:
        from generate_social_image import generate_social_image
    except ImportError as e:
        _emit(jid, "error", f"Cannot import generate_social_image: {e}")
        return

    scene_description = data.get("scene_description")
    scene_b64   = (data.get("message", {}).get("image") or {}).get("base64")
    scene_mime  = (data.get("message", {}).get("image") or {}).get("mime_type", "image/jpeg")
    scene_filename = (data.get("message", {}).get("image") or {}).get("filename", "scene.jpg")

    # Resolve products and their image files
    products = []
    img_files = []
    for name in product_names:
        p = next((x for x in catalogue if x["name"] == name), None)
        if not p:
            p = find_product(name, catalogue)
        if p:
            products.append(p)
            slug = re.sub(r"[^a-z0-9]+", "-", p["name"].lower()).strip("-")
            img_file = next(
                (f.name for f in PRODUCTS_DIR.glob("*.png")
                 if p["name"].lower() in f.stem.lower() or
                    (p.get("product_line") or "").lower() in f.stem.lower()),
                None
            )
            if img_file:
                img_files.append(str(PRODUCTS_DIR / img_file))

    if not products:
        _emit(jid, "error", "Could not find any of the requested products.")
        return

    names_str = ", ".join(_prompt_name(p["name"]) for p in products)
    progress(f"Building group shot prompt for: {names_str}")

    # Analyse scene
    scene_brief = None
    if scene_b64:
        progress("Analysing reference scene...")
        scene_brief = analyze_scene_image(scene_b64, scene_mime, f"arrange all these products in this scene: {names_str}")

    # Build a combined group prompt
    # Layer 1: scene (same as assemble_prompt)
    if scene_brief:
        keep   = scene_brief.get("keep", "")
        change = scene_brief.get("change", "")
        scene_prompt = scene_brief.get("scene_prompt", "")
        lighting = scene_brief.get("lighting", "")
        surfaces = scene_brief.get("surfaces", "")
        atmosphere = scene_brief.get("atmosphere", "")
        layer1 = (
            "IMAGE 1 (FIRST ATTACHED IMAGE) IS THE SCENE/STYLE REFERENCE — a real photograph.\n\n"
            f"SCENE ENVIRONMENT: {scene_prompt}\n\n"
            f"LIGHTING: {lighting}. SURFACES: {surfaces}. ATMOSPHERE: {atmosphere}.\n\n"
            f"PRESERVE EXACTLY: {keep}. CHANGE ONLY: Replace the existing products with the new Dr Gee products listed below.\n\n"
            "Replicate every environmental detail of this reference photograph with photographic accuracy. "
            "Do NOT invent a new background — rebuild this exact scene."
        )
    elif scene_description:
        layer1 = (
            "IMAGE 1 (FIRST ATTACHED IMAGE) IS THE SCENE/STYLE REFERENCE. "
            "Replicate its background, lighting, surfaces, atmosphere, and props EXACTLY. "
            "Replace only the products — everything else stays identical."
        )
    else:
        layer1 = DEFAULT_STYLE

    # Layer 2: group product instructions
    product_descs = []
    for p in products:
        cache_entry = cache.get(re.sub(r"[^a-z0-9]+", "-", p["name"].lower()).strip("-"))
        desc = (cache_entry.get("visual_description", "") if cache_entry else "") or p.get("description", "")
        display = _prompt_name(p["name"])
        product_descs.append(f"- {display}: {desc[:200]}" if desc else f"- {display}")

    products_block = "\n".join(product_descs)
    sizing_note = _build_group_sizing_note(products)
    layer2 = (
        f"REMAINING ATTACHED IMAGES ARE THE {len(products)} PRODUCT REFERENCES — one per product.\n\n"
        "Reproduce EVERY packaging detail of each product IDENTICALLY — labels, text, colors, cap style, shape. "
        "Do NOT alter or redesign any product.\n\n"
        f"{sizing_note}"
        f"Arrange ALL {len(products)} products together in the scene, grouped naturally as they appear "
        "in the reference photo layout. Products should be positioned close together on the same surface, "
        "slightly overlapping or touching, at slightly different depths for a natural arrangement.\n\n"
        f"PRODUCTS TO INCLUDE:\n{products_block}\n\n"
        "ORIGINAL BACKGROUND REMOVAL — CRITICAL: Each product image was photographed against a plain studio "
        "backdrop. That backdrop must be COMPLETELY REMOVED. Place only the physical products into the scene.\n\n"
        "SEAMLESS SCENE INTEGRATION: Fully relight each product to match the scene — remove all studio rim lights, "
        "white specular highlights, and colored background reflections. Replace with scene-appropriate lighting.\n\n"
        "SCENE COLOR TEMPERATURE — CRITICAL: The color of specular highlights, ambient wrap light, and surface "
        "reflections on ALL product surfaces must match the scene's exact color temperature. Warm rustic/golden "
        "scenes (wood, afternoon light) = warm amber/golden highlights on all white plastic caps and surfaces — "
        "NEVER blue or cool-white. Applying blue or cool-toned studio highlights in a warm scene is a critical "
        "compositing error that makes products look pasted-in. Every surface that catches light must carry the "
        "scene's warm or cool hue consistently across all products.\n\n"
        "NO ADDITIONS TO LABELS OR PACKAGING — CRITICAL: Do NOT add any illustrations, icons, leaf motifs, herb "
        "graphics, decorative symbols, ornaments, or any embellishment to the product labels or packaging that are "
        "not physically visible in the attached reference product images. Do NOT render any version numbers, "
        "numerals, or suffixes on labels that do not appear in the reference photo. Every label must be "
        "reproduced with absolute fidelity — nothing added, nothing removed, nothing re-styled."
    )

    # Layer 3: composition
    layer3 = (
        f"COMPOSITION: {len(products)} Dr Gee herbal wellness products arranged as a product group. "
        "3:4 portrait format. Products fill the lower 60% of the frame. "
        "Background scene visible in the upper 40%. "
        "Photorealistic commercial product photography — studio-grade clarity and detail."
    )

    full_prompt = f"{layer1}\n\n{layer2}\n\n{layer3}"

    # Use first product's slug for filename
    first_slug = re.sub(r"[^a-z0-9]+", "-", products[0]["name"].lower()).strip("-")
    group_slug = f"group-{len(products)}products-{first_slug}"

    try:
        result = generate_social_image(
            prompt=full_prompt,
            product_slug=group_slug,
            product_image_files=img_files if img_files else None,
            output_dir=str(GENERATED_DIR),
            scene_image_base64=scene_b64,
            scene_image_filename=scene_filename,
            on_progress=progress,
        )
        local_path = result.get("local_path")
        image_url  = result.get("image_url")

        group_video_prompt = assemble_group_video_prompt(products, scene_brief=scene_brief, scene_desc=scene_description or "premium wellness setting")

        evt_payload = {
            "product_name": names_str,
            "slug": group_slug,
            "image_prompt": full_prompt,
            "video_prompt": group_video_prompt,
            "product": {"name": names_str, "slug": group_slug},
        }
        if image_url:
            evt_payload["image_url"] = image_url
        if local_path:
            fn = Path(local_path).name
            evt_payload["generated_image_url"] = f"/api/generated/{quote(fn)}"

        _emit(jid, "image_ready", f"Group shot ready", evt_payload)
        _emit(jid, "done", f"Done! Group image generated for {len(products)} products.", {
            "reply": f"Here's your group shot with all {len(products)} products.",
            "multi": False,
        })

    except Exception as e:
        traceback.print_exc()
        _emit(jid, "error", f"Generation failed: {str(e)[:100]}")


def _run_multi_product_chat_job(jid: str, data: dict, product_names: list):
    """Background thread — generates images for multiple products, emitting each result as it completes."""
    def progress(msg):
        _emit(jid, "step", msg)

    try:
        from generate_social_image import generate_social_image
    except ImportError as e:
        _emit(jid, "error", f"Cannot import generate_social_image: {e}")
        return

    scene_description = data.get("scene_description")
    scene_b64   = data.get("message", {}).get("image", {}).get("base64")
    scene_mime  = data.get("message", {}).get("image", {}).get("mime_type", "image/jpeg")
    scene_filename = data.get("message", {}).get("image", {}).get("filename", "scene-reference")

    # Analyse scene once for all products
    scene_brief = None
    scene_text = scene_description
    if scene_b64:
        progress("Analysing reference scene...")
        scene_brief = analyze_scene_image(scene_b64, scene_mime, "place product in this scene")
        if scene_brief:
            scene_text = scene_brief.get("scene_prompt", "reference scene")
        else:
            scene_text = scene_description or "user-uploaded reference scene"

    total = len(product_names)
    generated = 0

    for i, name in enumerate(product_names):
        progress(f"[{i+1}/{total}] Finding {name}...")

        product = next((p for p in catalogue if p["name"] == name), None)
        if not product:
            product = find_product(name, catalogue)
        if not product:
            progress(f"[{i+1}/{total}] {name} — not found, skipping")
            continue

        name = product["name"]
        price = product.get("price", 0)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        cache_entry = next(
            (ce for ce in cache.values() if ce.get("product_name", "").lower() == name.lower()),
            None,
        )
        img_file = find_image_for_product(name)

        progress(f"[{i+1}/{total}] Building prompt for {name}...")
        image_prompt = assemble_prompt(
            product, cache_entry, None, scene_text,
            img_file=img_file, scene_brief=scene_brief,
        )
        video_prompt = assemble_video_prompt(
            product, cache_entry, scene_text or "premium wellness setting",
            img_file=img_file, scene_brief=scene_brief,
        )

        local_path = None
        image_url = None
        try:
            result = generate_social_image(
                prompt=image_prompt,
                product_slug=slug,
                output_dir=str(GENERATED_DIR),
                product_image_files=[img_file] if img_file else None,
                scene_image_base64=scene_b64,
                scene_image_filename=scene_filename,
                on_progress=progress,
            )
            local_path = result.get("local_path")
            image_url = result.get("image_url")
            generated += 1
        except Exception as e:
            progress(f"[{i+1}/{total}] {name} error: {str(e)[:60]}")

        # Emit this product's result immediately so frontend can show it
        evt_payload = {
            "product_name": name,
            "price": price,
            "slug": slug,
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "product": {"name": name, "price": price, "slug": slug},
        }
        if image_url:
            evt_payload["image_url"] = image_url
        if local_path:
            fn = Path(local_path).name
            evt_payload["generated_image_url"] = f"/api/generated/{quote(fn)}"
        _emit(jid, "image_ready", f"{name} ready", evt_payload)

    _emit(jid, "done", f"Done! Generated {generated}/{total} products.", {
        "reply": f"Generated {generated} of {total} products.",
        "multi": True,
    })


def _process_request(jid: str, data: dict):
    """Background thread — runs the full pipeline and emits progress via SSE."""
    def progress(msg):
        _emit(jid, "step", msg)

    try:
        msg_text = data.get("message", {}).get("text", "").strip()
        sender = data.get("sender", {}).get("name", "User")
        selected_product = data.get("selected_product", None)
        scene_description = data.get("scene_description", None)
        override_name = data.get("_override_product_name", None)

        progress("Received your request — looking up product...")

        # Find product — override_name wins (set by label version choice), then selected_product, then text match
        product = None
        if override_name:
            for p in catalogue:
                if p["name"] == override_name:
                    product = p
                    break
            if not product:
                product = find_product(override_name, catalogue)
        if not product and selected_product:
            for p in catalogue:
                if p["name"] == selected_product:
                    product = p
                    break
        if not product:
            product = find_product(msg_text, catalogue)

        if not product:
            _emit(jid, "error", "I couldn't identify a specific product. Try the catalogue browser or type the exact product name.")
            return

        name = product["name"]
        price = product.get("price", 0)
        progress(f"Found: {name} (R{price})")

        # Cache check
        cache_entry = None
        for slug_key, entry in cache.items():
            if entry.get("product_name", "").lower() == name.lower():
                cache_entry = entry
                break

        if cache_entry:
            progress("Cache hit — visual description loaded")
        else:
            progress("No cache entry — using catalogue description")

        # Extras
        extras = None
        extra_keywords = {
            "sale": "Add a bold sale badge in the upper-right corner.",
            "discount": "Add a discount percentage badge.",
            "new": "Add a 'NEW' badge in the upper-right corner.",
            "christmas": "Add subtle Christmas/festive decorative elements.",
            "winter": "Add subtle winter seasonal elements.",
            "summer": "Add bright, warm summer vibes to the background.",
        }
        for kw, extra in extra_keywords.items():
            if kw in msg_text.lower():
                extras = extra
                break

        # Extract scene image base64 (sent from simulator when user uploads a reference photo)
        scene_b64   = data.get("message", {}).get("image", {}).get("base64")
        scene_mime  = data.get("message", {}).get("image", {}).get("mime_type", "image/jpeg")
        scene_filename = data.get("message", {}).get("image", {}).get("filename", "scene-reference")

        # Run scene analyser when a reference image is present
        scene_brief = None
        scene_text  = scene_description  # plain-text fallback
        if scene_b64:
            progress("Analysing reference image...")
            scene_brief = analyze_scene_image(scene_b64, scene_mime, msg_text or "place the product in this scene")
            if scene_brief:
                progress(f"Scene analysis ready — keep: {scene_brief.get('keep','')[:40]}...")
                scene_text = scene_brief.get("scene_prompt", "reference scene")
            else:
                progress("Scene analysis unavailable — using image as direct reference")
                scene_text = scene_description or "user-uploaded reference scene"
        elif not scene_text and msg_text and len(msg_text) > len(name) + 10:
            # No image — use user's typed request as scene context
            scene_text = msg_text

        img_file = find_image_for_product(name)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        # Assemble prompts
        progress("Building 7-layer image prompt...")
        image_prompt = assemble_prompt(product, cache_entry, extras, scene_text, img_file=img_file, scene_brief=scene_brief)
        progress("Building video prompt (v3 locked-elements)...")
        video_prompt = assemble_video_prompt(product, cache_entry, scene_text or "premium wellness setting", img_file=img_file, scene_brief=scene_brief)

        print(f"[Job {jid}] Product: {name} (R{price}) | Cache: {'hit' if cache_entry else 'miss'} | Ref: {img_file or 'none'} | Scene: {'yes' if scene_b64 else 'no'}")

        # Stream prompts immediately — user sees the thinking while generation runs
        _emit(jid, "prompt_ready", "Prompts ready — generating image...", {
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "product": {"name": name, "price": price, "slug": slug},
        })

        # Generate image
        image_url = None
        local_path = None
        notion_url = None
        scene_imgbb_url = None

        try:
            from generate_social_image import generate_social_image
            ref_files = [img_file] if img_file else None
            result = generate_social_image(
                prompt=image_prompt,
                product_slug=slug,
                output_dir=str(GENERATED_DIR),
                product_image_files=ref_files,
                scene_image_base64=scene_b64,
                scene_image_filename=scene_filename,
                on_progress=progress,
            )
            local_path = result.get("local_path")
            image_url = result.get("image_url")
            scene_imgbb_url = result.get("scene_imgbb")
            if image_url:
                progress("Image saved locally")
        except Exception as e:
            print(f"[Job {jid}] Generation error: {e}")
            progress(f"Generation error: {str(e)[:80]}")

        # Notion logging
        progress("Logging to Notion...")
        ref_images = [img_file] if img_file else []
        if scene_imgbb_url:
            ref_images = [f"scene:{scene_imgbb_url}"] + ref_images
        try:
            from post_to_notion import post_to_notion
            notion_url = post_to_notion(
                product_name=name,
                image_url=image_url,
                local_path=local_path or "N/A",
                image_prompt=image_prompt,
                video_prompt=video_prompt,
                reference_images=ref_images,
            )
        except Exception as e:
            print(f"[Job {jid}] Notion error: {e}")

        # Build final payload
        reply_parts = [f"*{name}* (R{price})"]
        if image_url:
            reply_parts.append("Image generated successfully")
        else:
            reply_parts.append("Prompt ready — generation may have failed")
        if notion_url:
            reply_parts.append(f"Notion: {notion_url}")
        reply_parts.append(f"Cache: {'Cached' if cache_entry else 'Live lookup'}")

        payload = {
            "reply": "\n".join(reply_parts),
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "product": {"name": name, "price": price, "slug": slug},
        }
        if image_url:
            payload["image_url"] = image_url
        if local_path:
            fn = Path(local_path).name
            payload["generated_image_url"] = f"/api/generated/{quote(fn)}"
        if notion_url:
            payload["notion_url"] = notion_url

        payload["request_feedback"] = True
        _emit(jid, "done", "Done!", payload)

        # Set feedback state so next message from this sender is treated as feedback
        _set_session(sender, {
            "awaiting_feedback": True,
            "last_product": name,
            "last_slug": slug,
            "last_notion_url": notion_url,
        })

    except Exception as e:
        traceback.print_exc()
        _emit(jid, "error", f"Unexpected error: {str(e)}")


# ---------------------------------------------------------------------------
# Feedback job
# ---------------------------------------------------------------------------

def _handle_feedback_job(jid: str, sender: str, feedback_text: str):
    """Background thread — saves user feedback to Notion, clears session state."""
    def progress(msg):
        _emit(jid, "step", msg)

    session = _get_session(sender)
    product_name = session.get("last_product", "Unknown product")
    notion_url   = session.get("last_notion_url")

    # Clear feedback state immediately — next message is a new request
    _clear_session(sender, "awaiting_feedback", "last_product", "last_slug", "last_notion_url")

    is_positive = any(kw in feedback_text.lower() for kw in _FEEDBACK_POSITIVE)

    # Write feedback to Notion
    if notion_url:
        progress("Saving your feedback to Notion...")
        try:
            from post_to_notion import append_feedback_to_notion_page
            import os as _os
            api_key = _os.environ.get("NOTION_API_KEY")
            if api_key:
                # Extract 32-char hex page ID from the Notion URL
                m = re.search(r"([0-9a-f]{32})$", notion_url.rstrip("/").split("?")[0])
                if m:
                    append_feedback_to_notion_page(api_key, m.group(1), feedback_text, product_name)
                    progress("Feedback saved to Notion")
        except Exception as e:
            progress(f"Could not write to Notion: {str(e)[:60]}")

    if is_positive:
        reply = (
            f"Great to hear! Approval for *{product_name}* has been noted in Notion. "
            f"What would you like to generate next?"
        )
    else:
        reply = (
            f"Got it — your feedback on *{product_name}* has been saved in Notion. "
            f"Next time I generate this product I'll check those notes first and adjust the prompt. "
            f"What would you like to generate next?"
        )

    _emit(jid, "done", reply, {"reply": reply, "feedback_saved": True})


# ---------------------------------------------------------------------------
# Business Updater API
# ---------------------------------------------------------------------------

def _load_business_state(business_id: str) -> dict | None:
    path = BUSINESS_STATE_DIR / f"{business_id}.state.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _save_business_state(business_id: str, state: dict):
    path = BUSINESS_STATE_DIR / f"{business_id}.state.json"
    state["last_updated"] = datetime.now().isoformat() + "Z"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def _log_operation(entry: dict):
    OPERATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(OPERATIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


@app.route("/api/business", methods=["GET"])
def api_get_business():
    business_id = request.args.get("id", "dr-gee")
    state = _load_business_state(business_id)
    if not state:
        return jsonify({"error": f"Business '{business_id}' not found"}), 404
    return jsonify(state)


@app.route("/api/business/update", methods=["POST"])
def api_update_business():
    data = request.get_json(silent=True) or {}
    business_id = data.get("business_id", "dr-gee")
    updates = data.get("updates", {})
    category = data.get("category", "general")

    if not updates:
        return jsonify({"error": "No updates provided"}), 400

    state = _load_business_state(business_id)
    if not state:
        return jsonify({"error": f"Business '{business_id}' not found"}), 404

    changes = {}

    def _apply(target: dict, patch: dict, path=""):
        for key, new_val in patch.items():
            old_val = target.get(key)
            if isinstance(new_val, dict) and isinstance(old_val, dict):
                _apply(target[key], new_val, path + key + ".")
            else:
                if old_val != new_val:
                    changes[path + key] = {"before": old_val, "after": new_val}
                target[key] = new_val

    # Handle special array operations for promotions
    if "add_promotion" in updates:
        promo = updates.pop("add_promotion")
        if "id" not in promo:
            promo["id"] = f"promo-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        state.setdefault("active_promotions", []).append(promo)
        changes["active_promotions"] = {"action": "added", "promo": promo}

    if "expire_promotion" in updates:
        promo_id = updates.pop("expire_promotion")
        active = state.get("active_promotions", [])
        expired = [p for p in active if p.get("id") == promo_id]
        state["active_promotions"] = [p for p in active if p.get("id") != promo_id]
        state.setdefault("past_promotions", []).extend(expired)
        changes["active_promotions"] = {"action": "expired", "promo_id": promo_id}

    # Apply all remaining field updates
    _apply(state, updates)

    _save_business_state(business_id, state)

    _log_operation({
        "timestamp": datetime.now().isoformat() + "Z",
        "business_id": business_id,
        "operation": "business_update",
        "category": category,
        "changes": changes,
        "source": "form",
        "confirmed_by": "owner",
    })

    # Trigger showcase generation for brand/product/image_gen changes
    job_id = None
    if category in ("brand", "product", "image_gen") and changes:
        existing_images = set()
        if GENERATED_DIR.exists():
            for f in GENERATED_DIR.glob("*.png"):
                if "_social_" in f.stem:
                    existing_images.add(f.stem.split("_social_")[0])
        slugs_to_gen = [slug for slug in cache if slug not in existing_images]
        if slugs_to_gen:
            jid, _ = _new_job()
            job_id = jid
            threading.Thread(
                target=_run_showcase_gen_job, args=(jid, slugs_to_gen), daemon=True
            ).start()

    response = {"success": True, "changes": changes, "state": state}
    if job_id:
        response["job_id"] = job_id
    return jsonify(response)


@app.route("/api/generate-batch", methods=["POST"])
def api_generate_batch():
    """Trigger generation for all cached products. Returns job_id immediately.
    Query param: ?force=1 to regenerate even if images already exist."""
    force = request.args.get("force", "0") == "1"
    existing_images = set()
    if not force and GENERATED_DIR.exists():
        for f in GENERATED_DIR.glob("*.png"):
            if "_social_" in f.stem:
                existing_images.add(f.stem.split("_social_")[0])
    slugs_to_gen = [slug for slug in cache if force or slug not in existing_images]
    if not slugs_to_gen:
        return jsonify({
            "message": "All cached products already have images. Use ?force=1 to regenerate.",
            "count": 0,
        })
    jid, _ = _new_job()
    threading.Thread(
        target=_run_showcase_gen_job, args=(jid, slugs_to_gen), daemon=True
    ).start()
    return jsonify({"job_id": jid, "count": len(slugs_to_gen), "slugs": slugs_to_gen})


@app.route("/api/business/analyze-upload", methods=["POST"])
def api_analyze_brand_upload():
    """Analyze an uploaded brand document/image and extract structured data."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return jsonify({"error": "OPENROUTER_API_KEY not configured"}), 503

    data = request.get_json(silent=True) or {}
    b64_data = data.get("file_data", "")
    mime_type = data.get("mime_type", "image/png")
    file_name = data.get("file_name", "upload")

    if not b64_data:
        return jsonify({"error": "No file data provided"}), 400

    if "," in b64_data:
        b64_data = b64_data.split(",", 1)[1]

    BRAND_ANALYSIS_SYSTEM = """You are a CWS business analyst. Your job is to extract structured business profile data from uploaded documents, images, brand guides, packaging photos, or any brand material.

Extract everything you can find and return ONLY a JSON object with these fields (omit fields you cannot find):
{
  "display_name": "business display name",
  "brand_label_text": "uppercase label text if different",
  "tone_of_voice": "how the brand communicates",
  "target_market": "who the products are for",
  "brand_story": "origin or mission statement",
  "colors": {
    "primary": "#hex or null",
    "secondary": "#hex or null",
    "accent": "#hex or null"
  },
  "products": [
    {"name": "product name", "price": 350, "description": "short description"}
  ],
  "contacts": {
    "phone": "number or null",
    "email": "email or null",
    "website": "url or null",
    "instagram": "handle or null",
    "facebook": "page or null"
  },
  "delivery_methods": ["list of delivery options found"],
  "payment_methods": ["list of payment options found"],
  "other_notes": "any other relevant business info not captured above"
}

If the document is a product image, extract the product name, packaging description, and any visible text from the label."""

    # For PDFs: extract text and send as text content (vision API doesn't accept PDFs)
    is_pdf = mime_type == "application/pdf" or file_name.lower().endswith(".pdf")
    user_content = []

    if is_pdf:
        try:
            import pypdf, io
            pdf_bytes = base64.b64decode(b64_data)
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            pages_text = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages_text.append(f"--- Page {i+1} ---\n{text.strip()}")
            extracted_text = "\n\n".join(pages_text) if pages_text else ""
            if not extracted_text.strip():
                # PDF has no extractable text (e.g. scanned/image-only) — fall back to image
                is_pdf = False
                print(f"  [Brand Analyser] PDF has no text layer — falling back to image vision for {file_name}")
            else:
                print(f"  [Brand Analyser] PDF text extracted: {len(extracted_text)} chars across {len(reader.pages)} pages")
                user_content = [{"type": "text", "text": (
                    f"Analyse this brand document ({file_name}). "
                    f"Extracted text from the PDF:\n\n{extracted_text[:8000]}"
                )}]
        except Exception as e:
            print(f"  [Brand Analyser] PDF extraction error: {e} — falling back to image vision")
            is_pdf = False

    if not is_pdf:
        user_content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}
            },
            {"type": "text", "text": f"Analyse this brand document/image ({file_name}) and extract all business profile data you can find."},
        ]

    payload = {
        "model": SCENE_ANALYSIS_MODEL,
        "messages": [
            {"role": "system", "content": BRAND_ANALYSIS_SYSTEM},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 1200,
        "temperature": 0.2,
    }

    try:
        resp = _requests.post(
            OPENROUTER_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://cwspoeticartistry.github.io/cws-whatsapp-simulator/",
                "X-Title": "CWS Brand Analyser",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
        result = json.loads(raw)
        print(f"  [Brand Analyser] Extracted {len(result)} top-level fields from {file_name}")
        return jsonify({"success": True, "extracted": result})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Could not parse AI response: {e}", "raw": raw[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/business/recent-operations", methods=["GET"])
def api_recent_operations():
    business_id = request.args.get("id", "dr-gee")
    limit = int(request.args.get("limit", 20))
    if not OPERATIONS_LOG.exists():
        return jsonify([])
    entries = []
    with open(OPERATIONS_LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                    if entry.get("business_id") == business_id:
                        entries.append(entry)
                except Exception:
                    pass
    return jsonify(entries[-limit:])


@app.route("/api/generated-list", methods=["GET"])
def api_generated_list():
    limit = int(request.args.get("limit", 30))
    files = []
    if GENERATED_DIR.exists():
        for f in sorted(GENERATED_DIR.glob("*.png"), key=lambda x: x.stat().st_mtime, reverse=True):
            slug = f.stem.split("_social_")[0] if "_social_" in f.stem else f.stem
            files.append({"filename": f.name, "slug": slug, "url": f"/api/generated/{quote(f.name)}"})
    return jsonify(files[:limit])


@app.route("/sim", methods=["GET"])
@app.route("/sim/", methods=["GET"])
def simulator_ui():
    """Serve the simulator index.html over HTTP so phones on the same WiFi
    can make API calls without HTTPS → HTTP mixed-content blocks."""
    return send_from_directory(Path(__file__).parent, "index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("CWS_PORT", 5055))
    print(f"\n{'='*60}")
    print(f"  CWS Simulator Workspace — Local Server")
    print(f"  http://0.0.0.0:{port}")
    print(f"  Webhook: http://localhost:{port}/webhook")
    print(f"  Catalogue API: http://localhost:{port}/api/catalogue")
    print(f"{'='*60}\n")
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"  Phone (simulator): http://{local_ip}:{port}/sim")
        print(f"  Webhook:           http://{local_ip}:{port}/webhook\n")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=port, debug=False)
