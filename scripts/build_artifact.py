"""Build a self-contained copy of the landing page (inline CSS/JS, embedded connector list) for static hosting."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tessera import connectors, saas  # noqa: E402

S = Path(__file__).resolve().parents[1] / "tessera" / "static"
out = Path(sys.argv[1] if len(sys.argv) > 1 else "landing-standalone.html")
html = (S / "index.html").read_text()
body = html.split("<body>", 1)[1].split("</body>", 1)[0]
body = body.replace('<script src="/static/landing.js"></script>', "")
body = body.replace('href="/app"', 'href="#install"').replace(">Open Tessera<", ">Install free<").replace(">Open your Tessera<", ">See what stands out<", 1)
body = body.replace('<a class="btn" href="#install">See what stands out</a>', '<a class="btn" href="#standout">See what stands out</a>')
css = re.sub(r"@font-face\{[^}]*\}\s*", "", (S / "landing.css").read_text())  # fonts come from Google Fonts here
cat = {"types": [{k: v for k, v in t.items() if k in ("type", "label", "category", "status")} for t in connectors.catalog()],
       "via_generic": [{"name": n, "via": v} for n, v in connectors.VIA_GENERIC], "roadmap": connectors.ROADMAP,
       "apps": [{"label": a["label"], "category": a["category"], "changes_only": bool(a["incremental"])} for a in saas.catalog()]}
page = f"""<title>Tessera</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Instrument+Sans:ital,wdth,wght@0,75..100,400..700;1,75..100,400..700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
{css}
</style>
{body}
<script>window.TESSERA_CONNECTORS = {json.dumps(cat)};</script>
<script>
{(S / "landing.js").read_text()}
</script>
"""
out.write_text(page)
print(out, len(page))
