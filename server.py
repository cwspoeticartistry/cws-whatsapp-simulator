"""
CWS WhatsApp Simulator — Local Webhook Server
-----------------------------------------------
A Flask server that receives messages from the WhatsApp simulator web app
and processes them through the existing image generation workflow.

This acts as a local stand-in for the n8n webhook, running the same logic:
1. Receive message from simulator
2. Identify product from message text
3. Look up catalogue + product cache
4. Assemble image + video prompts (7-layer structure)
5. Generate image via kie.ai API
6. Log to Notion
7. Return response to simulator

Usage:
    cd "C:\\Users\\Dell\\Documents\\Dad\\Promo Images\\whatsapp-simulator"
    "C:\\Users\\Dell\\Documents\\Website Development\\.venv\\Scripts\\python.exe" server.py

The server runs on http://0.0.0.0:5055 — accessible from your phone
on the same WiFi network via http://<your-computer-ip>:5055/webhook
"""

import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify
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

# Add scripts dir to path so we can import the generation/notion modules
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
# Load catalogue and cache
# ---------------------------------------------------------------------------
def load_catalogue():
    """Parse dr-gee-catalogue.md into a searchable list."""
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
    """Load product-prompt-cache.json."""
    cache_path = REFS_DIR / "product-prompt-cache.json"
    if cache_path.exists():
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def find_product(text, catalogue):
    """Find best matching product from message text."""
    text_lower = text.lower().strip()

    # Direct name match
    for p in catalogue:
        if p["name"].lower() in text_lower:
            return p

    # Word overlap scoring
    text_words = set(re.findall(r"\w+", text_lower))
    best_score = 0
    best_match = None

    for p in catalogue:
        name_words = set(re.findall(r"\w+", p["name"].lower()))
        # Remove common words
        name_words -= {"and", "the", "for", "with", "of", "a", "in"}
        overlap = len(text_words & name_words)
        if overlap > best_score and overlap >= 1:
            best_score = overlap
            best_match = p

    return best_match


# ---------------------------------------------------------------------------
# Prompt assembly (mirrors SKILL.md 7-layer structure)
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


def assemble_prompt(product, cache_entry=None, extras=None):
    """Assemble the 7-layer image prompt."""
    name = product["name"]
    price = product.get("price", 0)
    description = product.get("description", "")

    # Layer 1 — Style
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

    # Layer 3 — Composition
    layer3 = DEFAULT_COMPOSITION

    # Layer 4 — Text overlay
    layer4 = (
        "CRITICAL — the following text must appear letter-perfect in the image with "
        "no alterations, misspellings, or creative reinterpretation:\n"
        f"Product name: {name}\n"
        f"Price: R{price}\n"
        "Brand: Dr Gee"
    )

    # Layer 5 — Additional elements
    layer5 = extras if extras else ""

    # Layer 6 — Technical
    layer6 = TECHNICAL_SPECS

    # Combine
    layers = [layer1, layer2, layer3, layer4]
    if layer5:
        layers.append(layer5)
    layers.append(layer6)

    return "\n\n".join(layers)


def assemble_video_prompt(product, scene_desc="deep teal gradient background"):
    """Generate companion video prompt."""
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
CORS(app)  # Allow simulator to call from any origin

catalogue = load_catalogue()
cache = load_cache()

print(f"\n[CWS Server] Loaded {len(catalogue)} products from catalogue")
print(f"[CWS Server] Loaded {len(cache)} cached product descriptions")


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "CWS WhatsApp Simulator Webhook",
        "status": "running",
        "products_loaded": len(catalogue),
        "cached_products": len(cache),
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    """Main webhook endpoint — processes simulator messages."""
    try:
        data = request.get_json(force=True)
        msg_text = data.get("message", {}).get("text", "").strip()
        msg_type = data.get("message", {}).get("type", "text")
        sender = data.get("sender", {}).get("name", "User")
        session_id = data.get("session_id", "unknown")

        print(f"\n[Webhook] From {sender}: {msg_text[:80]}")

        if not msg_text:
            return jsonify({"reply": "I received your message but it was empty. Please tell me which product you'd like a promo image for."})

        # Check if it's a greeting or general message
        greetings = ["hi", "hello", "hey", "howzit", "good morning", "good afternoon"]
        if msg_text.lower().strip() in greetings:
            product_list = "\n".join([f"  - {p['name']} (R{p.get('price', '?')})" for p in catalogue[:10]])
            return jsonify({
                "reply": (
                    f"Hello {sender}! Welcome to the Dr Gee promo image generator.\n\n"
                    f"Tell me which product you'd like a social media image for. "
                    f"For example: 'Create a promo image for Blood Circulation'\n\n"
                    f"Here are some products:\n{product_list}\n\n"
                    f"...and {len(catalogue) - 10} more. Just name any Dr Gee product!"
                )
            })

        # Check for "list" or "products" or "catalogue"
        if any(kw in msg_text.lower() for kw in ["list", "products", "catalogue", "catalog", "all products"]):
            product_list = "\n".join([f"  - {p['name']} (R{p.get('price', '?')})" for p in catalogue])
            return jsonify({"reply": f"Dr Gee Product Catalogue:\n\n{product_list}"})

        # Try to find a product
        product = find_product(msg_text, catalogue)

        if not product:
            return jsonify({
                "reply": (
                    f"I couldn't identify a specific product from your message. "
                    f"Could you tell me the exact product name?\n\n"
                    f"Try something like:\n"
                    f"  'Create a promo for Blood Circulation'\n"
                    f"  'Make a social image for Shilajit Capsules'\n"
                    f"  'Promo image for QS8 Daily Support'"
                )
            })

        # Product found — assemble prompt
        name = product["name"]
        price = product.get("price", 0)
        sku = product.get("sku", "")

        # Find cache entry
        cache_entry = None
        for slug, entry in cache.items():
            if entry.get("product_name", "").lower() == name.lower():
                cache_entry = entry
                break

        # Check for extras in message
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

        # Assemble prompts
        image_prompt = assemble_prompt(product, cache_entry, extras)
        video_prompt = assemble_video_prompt(product)

        # Build slug
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        print(f"[Webhook] Product: {name} (R{price})")
        print(f"[Webhook] Cache: {'hit' if cache_entry else 'miss'}")
        print(f"[Webhook] Prompt length: {len(image_prompt)} chars")

        # Try to generate image
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
            print(f"[Webhook] Generation: {gen_status}")
        except Exception as e:
            print(f"[Webhook] Generation error: {e}")
            gen_status = "error"

        # Try Notion logging
        try:
            from post_to_notion import post_to_notion
            notion_url = post_to_notion(
                product_name=name,
                image_url=image_url,
                local_path=local_path or "N/A",
                image_prompt=image_prompt,
                video_prompt=video_prompt,
                reference_images=[],
            )
            print(f"[Webhook] Notion: {notion_url}")
        except Exception as e:
            print(f"[Webhook] Notion error: {e}")

        # Build response
        reply_parts = [
            f"Promo image request for *{name}* (R{price}) received!",
            "",
        ]

        if gen_status == "generated":
            reply_parts.append(f"Image generated successfully!")
            reply_parts.append(f"Saved to: {local_path}")
        elif gen_status == "placeholder":
            reply_parts.append("Image generation completed (placeholder saved).")
            reply_parts.append(f"File: {local_path}")
        elif gen_status == "error":
            reply_parts.append("Image generation encountered an error. Prompt is ready for manual use.")
        else:
            reply_parts.append("Prompt assembled and ready.")

        if notion_url:
            reply_parts.append(f"\nNotion page: {notion_url}")

        reply_parts.append(f"\nCache status: {'Cached' if cache_entry else 'Not cached — visual description from catalogue'}")
        reply_parts.append(f"Prompt length: {len(image_prompt)} characters")

        response = {
            "reply": "\n".join(reply_parts),
        }

        if image_url:
            response["image_url"] = image_url

        return jsonify(response)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"reply": f"Server error: {str(e)}"}), 500


@app.route("/products", methods=["GET"])
def list_products():
    """List all catalogue products."""
    return jsonify([
        {"name": p["name"], "price": p.get("price"), "sku": p.get("sku")}
        for p in catalogue
    ])


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("CWS_PORT", 5055))
    print(f"\n{'='*60}")
    print(f"  CWS WhatsApp Simulator — Local Webhook Server")
    print(f"  http://0.0.0.0:{port}")
    print(f"  Webhook URL: http://localhost:{port}/webhook")
    print(f"  (Use your computer's local IP for phone access)")
    print(f"{'='*60}\n")

    # Show local IP for phone access
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        print(f"  Phone URL: http://{local_ip}:{port}/webhook")
        print(f"  Simulator: open index.html, set webhook to above URL\n")
    except Exception:
        pass

    app.run(host="0.0.0.0", port=port, debug=False)
