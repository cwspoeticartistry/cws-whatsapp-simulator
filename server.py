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
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(r"C:\Users\Dell\Documents\Dad\Promo Images")
SKILL_ROOT = Path(r"C:\Users\Dell\.claude\skills\social-image-prompter")
SCRIPTS_DIR = SKILL_ROOT / "scripts"
REFS_DIR = SKILL_ROOT / "references"
PRODUCTS_DIR = PROJECT_ROOT / "products"
GENERATED_DIR = PROJECT_ROOT / "generated"
ENV_PATH = Path(r"C:\Users\Dell\Documents\Website Development\.env")

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
    "3:4 portrait aspect ratio composition. Product centered in the lower two-thirds "
    "occupying approximately 60% of image height. Generous negative space in the upper "
    "third for headline text. Straight-on front-facing shot with subtle 5-degree tilt. "
    "Product on a barely-visible reflective surface with soft mirror effect."
)

TECHNICAL_SPECS = (
    "3:4 portrait aspect ratio, 1080x1440 pixels. High resolution, professional product "
    "photography quality. Sharp focus on product with smooth background. Commercial "
    "advertising standard suitable for Instagram, Facebook, and WhatsApp marketing. "
    "No watermarks. No stock photo artifacts. Photorealistic rendering."
)


def assemble_prompt(product, cache_entry=None, extras=None, scene_desc=None):
    name = product["name"]
    price = product.get("price", 0)
    description = product.get("description", "")

    # Layer 1 — Style (use scene description if provided)
    if scene_desc:
        layer1 = f"Scene style based on the uploaded reference image: {scene_desc}. Maintain premium product photography quality with professional lighting."
    else:
        layer1 = DEFAULT_STYLE

    # Layer 2 — Product
    if cache_entry:
        visual_desc = cache_entry.get("visual_description", "")
        layer2 = (
            "The attached product image(s) are the exact visual reference for the "
            "product(s) that must appear in this image. These attached photos are the "
            "absolute ground truth for the shape, packaging design, label design, label "
            "text, branding, colors, cap style, and all physical details of the products. "
            "Reproduce every packaging detail IDENTICALLY — do not alter, redesign, "
            "reimagine, stylize, or change anything about the product itself.\n\n"
            "SEAMLESS SCENE INTEGRATION: The product photos were taken against a studio "
            "background and may have rim lighting, colored highlights, or background "
            "artifacts that do not match the new scene. Remove any background artifacts "
            "from the product photos and relight the products naturally to match the "
            "scene's lighting environment described above. The product surfaces, cap, "
            "and bottle should pick up soft shadows, ambient reflections, and the "
            "scene's light direction as if they were physically present in the scene.\n\n"
            f"REINFORCEMENT DESCRIPTION: {visual_desc}\n\n"
            "IMPORTANT: The attached product images are the definitive reference for "
            "all packaging details. The only things that should adapt to the new scene "
            "are: lighting on the product exterior, shadows cast by the product, and "
            "surface reflections beneath it."
        )
    else:
        layer2 = (
            f"The product is {name} by Dr Gee — a premium South African herbal "
            f"wellness product. {description}"
        )

    layer3 = DEFAULT_COMPOSITION
    layer4 = (
        "CRITICAL — the following text must appear letter-perfect in the image with "
        "no alterations, misspellings, or creative reinterpretation:\n"
        f"Product name: {name}\n"
        f"Price: R{price}\n"
        "Brand: Dr Gee"
    )
    layer5 = extras if extras else ""
    layer6 = TECHNICAL_SPECS

    layers = [layer1, layer2, layer3, layer4]
    if layer5:
        layers.append(layer5)
    layers.append(layer6)
    return "\n\n".join(layers)


def assemble_video_prompt(product, scene_desc="deep teal gradient background"):
    name = product["name"]
    return (
        f"A smooth, slow cinematic product video for Dr Gee herbal wellness. The scene "
        f"is identical to the reference image — {scene_desc} with {name} product "
        f"centered on a reflective surface. The products remain sharp, still, and "
        f"perfectly in focus throughout the entire video.\n\n"
        f"In the background, slightly out of frame at first, a well-dressed Black "
        f"South African woman in her 30s wearing a soft neutral linen top moves "
        f"naturally and unhurriedly in the background. She is always in soft bokeh "
        f"blur — never in focus — appearing as warm, authentic lifestyle atmosphere "
        f"rather than as the subject. She reaches past the products to pick up a cup. "
        f"Her presence is calm, natural, and effortless — the kind of person who uses "
        f"these products as part of a healthy daily routine.\n\n"
        f"The camera performs a very slow, smooth push-in toward the products — moving "
        f"approximately 10-15% closer over the duration of the clip. No shaking, no "
        f"cuts. The lighting remains consistent with soft front-left diffused studio "
        f"light. The overall mood is premium wellness lifestyle — warm, aspirational, "
        f"and trustworthy.\n\n"
        f"Duration: 6-8 seconds. Aspect ratio: 3:4 portrait. Cinematic colour grade: "
        f"slightly warm and clean. No music needed."
    )


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

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
    """Main chat webhook — processes simulator messages."""
    try:
        data = request.get_json(force=True)
        msg_text = data.get("message", {}).get("text", "").strip()
        msg_image = data.get("message", {}).get("image", None)
        sender = data.get("sender", {}).get("name", "User")
        selected_product = data.get("selected_product", None)
        scene_description = data.get("scene_description", None)

        print(f"\n[Webhook] From {sender}: {msg_text[:80]}")
        if selected_product:
            print(f"[Webhook] Pre-selected product: {selected_product}")

        if not msg_text and not selected_product:
            return jsonify({"reply": "I received your message but it was empty. Please tell me which product you'd like a promo image for, or use the catalogue to select one."})

        # Greetings
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

        # List products
        if any(kw in msg_text.lower() for kw in ["list", "products", "catalogue", "catalog", "all products"]):
            product_list = "\n".join([f"  - {p['name']} (R{p.get('price', '?')})" for p in catalogue])
            return jsonify({"reply": f"Dr Gee Product Catalogue:\n\n{product_list}"})

        # Find product — either pre-selected from catalogue or from text
        product = None
        if selected_product:
            for p in catalogue:
                if p["name"] == selected_product:
                    product = p
                    break
        if not product:
            product = find_product(msg_text, catalogue)

        if not product:
            return jsonify({
                "reply": (
                    "I couldn't identify a specific product from your message.\n\n"
                    "Try using the catalogue button to browse products with images, "
                    "or type the exact product name."
                )
            })

        # Product found
        name = product["name"]
        price = product.get("price", 0)

        # Find cache entry
        cache_entry = None
        for slug_key, entry in cache.items():
            if entry.get("product_name", "").lower() == name.lower():
                cache_entry = entry
                break

        # Check for extras
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

        # Use user's message as scene enhancement if not just a product name
        scene_text = scene_description
        if not scene_text and msg_text and len(msg_text) > len(name) + 10:
            scene_text = msg_text

        # Assemble prompts
        image_prompt = assemble_prompt(product, cache_entry, extras, scene_text)
        video_prompt = assemble_video_prompt(product, scene_text or "deep teal gradient background")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        print(f"[Webhook] Product: {name} (R{price})")
        print(f"[Webhook] Cache: {'hit' if cache_entry else 'miss'}")
        print(f"[Webhook] Scene: {scene_text[:60] if scene_text else 'default'}")

        # Generate image
        image_url = None
        local_path = None
        notion_url = None
        gen_status = "prompt_only"

        try:
            from generate_social_image import generate_social_image
            print(f"[Webhook] Starting kie.ai generation...")
            result = generate_social_image(
                prompt=image_prompt,
                product_slug=slug,
                output_dir=str(GENERATED_DIR),
            )
            local_path = result.get("local_path")
            image_url = result.get("image_url")
            gen_status = "generated" if image_url else "placeholder"
        except Exception as e:
            print(f"[Webhook] Generation error: {e}")
            gen_status = "error"

        # Notion logging
        img_file = find_image_for_product(name)
        ref_images = [img_file] if img_file else []
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
            print(f"[Webhook] Notion error: {e}")

        # Build response
        reply_parts = [f"*{name}* (R{price}) — Promo image request processed!", ""]
        if gen_status == "generated":
            reply_parts.append("Image generated successfully!")
        elif gen_status == "placeholder":
            reply_parts.append("Placeholder saved (generation pending).")
        elif gen_status == "error":
            reply_parts.append("Generation error — prompt ready for manual use.")

        if notion_url:
            reply_parts.append(f"\nNotion: {notion_url}")
        reply_parts.append(f"\nCache: {'Cached visual description' if cache_entry else 'Using catalogue description'}")

        response = {
            "reply": "\n".join(reply_parts),
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "product": {"name": name, "price": price, "slug": slug},
        }
        if image_url:
            response["image_url"] = image_url
        if local_path:
            fn = Path(local_path).name
            response["generated_image_url"] = f"/api/generated/{quote(fn)}"

        return jsonify(response)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"reply": f"Server error: {str(e)}"}), 500


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
        print(f"  Phone: http://{local_ip}:{port}\n")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=port, debug=False)
