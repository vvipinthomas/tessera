"""Browser smoke test: drives the real server in Chromium through every screen.

Run:  python tests/ui_smoke.py [--shots DIR]
Starts its own server on a free port with a temporary data folder. Fails on any console error or failed request.
"""
import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PW = "correct horse battery 9"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def features(b, base, shots, problems):
    """Features end to end: plain-English questions, every visual, the designer, sharing, the tour and the demo switch."""
    ctx = b.new_context(viewport={"width": 1360, "height": 900})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    pg.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and problems.append(f"console: {m.text}"))

    def shot(name):
        if shots:
            pg.screenshot(path=str(shots / f"feature-{name}.png"), full_page=True, timeout=15000)

    def step(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            problems.append(f"feature {name}: {str(e).splitlines()[0]}")
            try:
                pg.screenshot(path=str(shots / f"FAILED-{name}.png"), timeout=8000) if shots else None
            except Exception:  # noqa: BLE001
                pass

    pg.goto(base + "/app")
    pg.fill("#li-email", "admin@acme.ca")
    pg.fill("#li-pw", PW)
    pg.click("button[type=submit]")
    pg.wait_for_selector("text=AI readiness", timeout=30000)
    pg.wait_for_selector("#demo-pill", timeout=20000)

    def ask():
        pg.goto(base + "/app#/explore/1")
        pg.wait_for_selector("#ex-ask")
        pg.fill("#ex-ask", "top 5 provinces by sales last quarter")
        pg.keyboard.press("Enter")
        pg.wait_for_selector("text=Understood as:", timeout=10000)
        pg.wait_for_selector("#main svg.chart rect", timeout=10000)
        assert "top 5" in pg.inner_text(".askcard")
        shot("ask")
    step("ask", ask)

    def visuals():
        pg.goto(base + "/app#/explore/1")
        pg.wait_for_selector("#f-dimensions-province")
        pg.check("#f-dimensions-province")
        for label, sel in (("Treemap", "svg.treemap"), ("Waterfall", "svg[aria-label^='Waterfall']"), ("Funnel", "svg[aria-label^='Funnel']"), ("Bar", "svg[aria-label^='Bar chart']")):
            pg.click(f".seg >> text={label}")
            pg.wait_for_selector(f"#main {sel}", timeout=10000)
            shot("viz-" + label.lower())
        pg.check("#f-measures-orders")
        pg.click(".seg >> text=Combo")
        pg.wait_for_selector("#main svg[aria-label*='by Province']", timeout=10000)
        shot("viz-combo")
        pg.click(".seg >> text=Scatter")
        pg.wait_for_selector("#main svg[aria-label*='against']", timeout=10000)
        shot("viz-scatter")
        pg.uncheck("#f-measures-orders")
        pg.uncheck("#f-dimensions-province")
        pg.check("#f-dimensions-order_date")
        pg.check("#f-dimensions-channel")
        pg.click(".seg >> text=Lines")
        pg.wait_for_selector("#main svg[aria-label*='over time by']", timeout=10000)
        shot("viz-lines")
    step("visuals", visuals)

    def palette_ask():
        pg.goto(base + "/app#/overview")
        pg.keyboard.press("Control+k")
        pg.fill("#palette-q", "monthly revenue in BC")
        pg.wait_for_selector(".palette-item >> text=Ask: monthly revenue in BC", timeout=10000)
        pg.keyboard.press("Enter")
        pg.wait_for_selector("text=Understood as:", timeout=15000)
        assert "Province is BC" in pg.inner_text(".askcard")
    step("palette-ask", palette_ask)

    def designer():
        pg.goto(base + "/app#/dashboards/1/edit")
        pg.wait_for_selector(".tile.designable", timeout=15000)
        pg.wait_for_timeout(800)
        first = pg.locator(".tile.designable h3").first.inner_text()
        tiles = pg.locator(".tile.designable")
        a, c = tiles.nth(0).locator("h3").bounding_box(), tiles.nth(2).bounding_box()
        pg.mouse.move(a["x"] + 10, a["y"] + 5)
        pg.mouse.down()
        pg.mouse.move(a["x"] + 30, a["y"] + 20, steps=5)
        pg.mouse.move(c["x"] + c["width"] * 0.8, c["y"] + c["height"] / 2, steps=10)
        pg.mouse.up()
        pg.wait_for_timeout(800)
        if pg.locator(".tile.designable h3").first.inner_text() == first:
            raise AssertionError("drag did not reorder tiles")
        h = pg.locator(".tile.designable .resize").nth(1)
        box = h.bounding_box()
        pg.mouse.move(box["x"] + 5, box["y"] + 40)
        pg.mouse.down()
        pg.mouse.move(box["x"] + 300, box["y"] + 40, steps=8)
        pg.mouse.up()
        pg.wait_for_timeout(800)
        pg.wait_for_selector(".tile.designable", timeout=5000)
        shot("designer")
    step("designer", designer)

    def share():
        pg.goto(base + "/app#/admin/ops")
        pg.wait_for_selector("#share-on")
        pg.check("#share-on")
        pg.click("text=Save >> nth=-1")
        pg.wait_for_timeout(500)
        pg.goto(base + "/app#/dashboards/1")
        pg.click("button:has-text('Share')")
        pg.click("text=Create link")
        pg.wait_for_selector("dialog input[readonly]", timeout=10000)
        url = pg.locator("dialog input[readonly]").first.input_value()
        pg.keyboard.press("Escape")
        anon = b.new_context(viewport={"width": 1200, "height": 900})
        ep = anon.new_page()
        ep.on("pageerror", lambda e: problems.append(f"embed pageerror: {e}"))
        ep.goto(url)
        ep.wait_for_selector("svg.chart", timeout=20000)
        if shots:
            ep.screenshot(path=str(shots / "feature-embed.png"), full_page=True)
        anon.close()
    step("share", share)

    def connectors_form():
        pg.goto(base + "/app#/connections/new")
        pg.wait_for_selector("#main select")
        pg.select_option("#nc-type", "mongodb")
        pg.wait_for_selector("text=Connection string", timeout=5000)
    step("connectors", connectors_form)

    def tour_and_demo_switch():
        pg.goto(base + "/app#/overview")
        pg.click("#demo-pill >> text=Take the tour")
        pg.wait_for_selector("#tour", timeout=10000)
        for _ in range(12):
            pg.click("#tour-next")
            pg.wait_for_timeout(900)
        shot("tour")
        pg.click("#tour >> text=✕")
        pg.click("#demo-pill >> text=Switch to real data")
        pg.wait_for_selector("text=Remove demo data", timeout=10000)
        shot("demo-remove")
        pg.click("text=Remove demo data")
        pg.wait_for_selector("#demo-pill >> text=Load demo", timeout=20000)
        pg.goto(base + "/app#/dashboards")
        pg.wait_for_selector("text=No dashboards yet", timeout=10000)
        pg.click("#demo-pill >> text=Load demo")
        pg.click("dialog >> text=Load demo")
        pg.wait_for_selector("#tour", timeout=60000)
        pg.wait_for_selector("#demo-pill >> text=Demo data", timeout=20000)
    step("tour-demo", tour_and_demo_switch)
    ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="")
    a = ap.parse_args()
    shots = Path(a.shots) if a.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    data = tempfile.mkdtemp()
    port = free_port()
    env = {**os.environ, "TESSERA_DATA_DIR": data, "PYTHONPATH": str(ROOT)}
    with open(Path(data) / "server.log", "w+") as logf:  # closed on every path, even if the test crashes
        srv = subprocess.Popen([sys.executable, "-m", "tessera", "serve", "--port", str(port)], env=env, stdout=logf, stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{port}"
        problems = []
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            code = (Path(data) / "setup-code.txt").read_text().strip()
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM", None) or None)
                for scheme, vp in [("light", {"width": 1360, "height": 900}), ("dark", {"width": 390, "height": 844})]:
                    ctx = b.new_context(viewport=vp, color_scheme=scheme)
                    pg = ctx.new_page()
                    pg.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
                    pg.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
                    pg.on("requestfailed", lambda r: problems.append(f"requestfailed: {r.url}"))
                    pg.on("response", lambda r: r.status >= 500 and problems.append(f"HTTP {r.status}: {r.url}"))

                    def shot(name):
                        if shots:
                            pg.screenshot(path=str(shots / f"{scheme}-{name}.png"), full_page=True)

                    pg.goto(base + "/")
                    pg.wait_for_selector("#conn-list div")
                    shot("landing")
                    pg.goto(base + "/app")
                    if scheme == "light":
                        pg.wait_for_selector("#su-code")
                        shot("setup")
                        pg.fill("#su-code", code)
                        pg.fill("#su-name", "Vipin Thomas")
                        pg.fill("#su-email", "admin@acme.ca")
                        pg.fill("#su-pw", PW)
                        pg.click("button[type=submit]")
                    else:
                        pg.wait_for_selector("#li-email")
                        pg.fill("#li-email", "admin@acme.ca")
                        pg.fill("#li-pw", PW)
                        pg.click("button[type=submit]")
                    pg.wait_for_selector("text=AI readiness", timeout=60000)
                    pg.wait_for_timeout(300)
                    shot("overview")
                    routes = [("catalog", "Datasets"), ("catalog/3", "Readiness"), ("catalog/3/columns", "Masking"), ("catalog/3/preview", "First"),
                              ("catalog/3/profile", "Distinct"), ("catalog/3/quality", "Quality rules"), ("catalog/3/lineage", "Upstream"),
                              ("connections", "Connector catalog"), ("connections/1", "Copy many tables"), ("connections/app", "Salesforce"), ("connections/app/stripe", "Save and test"),
                              ("catalog/3/changes", "schema changes"), ("connections/new", "New connection"),
                              ("pipelines", "Retail daily load"), ("pipelines/1", "Steps"), ("pipelines/1/edit", "Edit pipeline"), ("pipelines/run/2", "Log"),
                              ("sql", "Run"), ("dashboards", "Sales overview"), ("dashboards/1", "Revenue by month"), ("agents", "Ask your data"),
                              ("admin/users", "Add user"), ("admin/tokens", "Create token"), ("admin/sso", "OpenID Connect"), ("admin/ai", "Add provider"), ("admin/audit", "audit"),
                              ("explore", "Measures"), ("explore/1", "Compare with previous period"), ("reports", "Category performance"), ("reports/1", "Excel"),
                              ("models", "Semantic models"), ("models/1/edit", "Measures"), ("glossary", "Refund rate"), ("notebooks", "Refund investigation"),
                              ("notebooks/1", "Run all"), ("transforms", "Visual transforms"), ("monitor", "Monitoring hub"), ("alerts", "Refund rate above 8%"),
                              ("admin/security", "User attributes"), ("admin/email", "SMTP host"), ("admin/deploy", "Download bundle"),
                              ("admin/mcp", "Model Context Protocol server"), ("admin/provisioning", "SCIM 2.0 provisioning"), ("admin/ops", "Active sessions"),
                              ("workspaces", "General"), ("workspaces/1", "Members"), ("brief/1", "full-year forecast"),
                              ("streams", "Store checkouts"), ("streams/1", "Throughput"), ("streams/new", "Create and start"),
                              ("admin/integrations", "OpenLineage")]
                    for r, text in routes:
                        pg.goto(f"{base}/app#/{r}")
                        try:
                            pg.wait_for_selector(f"#main >> text={text}", timeout=15000)
                        except Exception:
                            problems.append(f"[{scheme}] route {r}: did not show '{text}'")
                            if os.environ.get("SMOKE_DEBUG") and not getattr(main, "_dumped", False):
                                main._dumped = True
                                problems.append(subprocess.run(["py-spy", "dump", "--pid", str(srv.pid)], capture_output=True, text=True).stdout)
                        pg.wait_for_timeout(500 if r.startswith(("dashboards/", "explore", "transforms", "reports/", "brief/")) else 120)
                        if scheme == "light" or r in ("catalog/3", "dashboards/1", "pipelines/1", "sql", "explore/1", "monitor", "brief/1"):
                            shot(r.replace("/", "_"))
                        bad = pg.locator(".notice.bad").all_text_contents()
                        if bad:
                            problems.append(f"[{scheme}] route {r}: error notice {bad}")
                        if pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1") and r not in ("pipelines/1", "pipelines/run/2"):
                            problems.append(f"[{scheme}] route {r}: page scrolls horizontally")
                    if scheme == "light":
                        pg.goto(f"{base}/app#/sql")
                        pg.wait_for_selector("#sql-editor")
                        pg.fill("#sql-editor", "select province, count(*) n from serving.orders_enriched group by 1 order by 2 desc")
                        pg.click("text=Run  (Ctrl+Enter)")
                        pg.wait_for_selector("#viz-type")
                        pg.select_option("#viz-type", "bar")
                        pg.wait_for_selector("svg.chart")
                        shot("sql_chart")
                        # bulk copy: pick every file of the demo connection and copy them in one pipeline
                        pg.goto(f"{base}/app#/connections/1")
                        pg.click("text=Choose tables")
                        pg.wait_for_selector(".objgrid input")
                        pg.check("input[aria-label='Select all']")
                        pg.click("text=Copy selected")
                        pg.wait_for_selector("text=Open the pipeline", timeout=15000)
                        shot("bulk_copy")
                        pg.goto(f"{base}/app#/catalog/3/quality")
                        pg.click("text=Run checks")
                        pg.wait_for_selector("text=Score", timeout=15000)
                        # interactive dashboard: cross-filter by clicking a province bar, then reset
                        pg.goto(f"{base}/app#/dashboards/1")
                        pg.wait_for_selector("#main svg.chart rect.pickable", timeout=20000)
                        pg.locator("#main svg.chart rect.pickable").first.click()
                        pg.wait_for_selector(".crosschip", timeout=10000)
                        pg.wait_for_timeout(600)
                        shot("dashboard_crossfilter")
                        pg.click("text=Reset filters")
                        pg.wait_for_timeout(300)
                        if pg.locator(".crosschip").count():
                            problems.append("cross-filter chip did not clear")
                        # AI analysis: insights panel, explain a change, deep dive
                        pg.click("text=✦ Insights")
                        pg.wait_for_selector(".sidepanel .fact", timeout=30000)
                        pg.wait_for_timeout(300)
                        shot("dashboard_insights")
                        pg.locator(".sidepanel >> text=Explain this change →").first.click()
                        pg.wait_for_selector("dialog .wfrow", timeout=30000)
                        shot("explain_change")
                        pg.keyboard.press("Escape")
                        pg.locator(".tilemenu summary").nth(4).click()
                        pg.locator(".tilemenu[open] >> text=Deep dive").click()
                        pg.wait_for_selector("dialog .dnode select", timeout=30000)
                        pg.locator("dialog .dnode select").first.select_option("@high")
                        pg.wait_for_selector("dialog .dcol:nth-child(3) .dnode", timeout=30000)
                        shot("deep_dive")
                        pg.keyboard.press("Escape")
                        # explore: forecast and anomalies on a monthly line
                        pg.goto(f"{base}/app#/explore/1")
                        pg.wait_for_selector("#f-dimensions-order_date")
                        pg.check("#f-dimensions-order_date")
                        pg.check("#ex-forecast")
                        pg.check("#ex-anom")
                        pg.wait_for_selector("#main svg.chart path[stroke-dasharray]", timeout=15000)
                        shot("explore_forecast")
                        # explore: add a dimension, switch visual
                        pg.goto(f"{base}/app#/explore/1")
                        pg.reload()
                        pg.wait_for_selector("#f-dimensions-province")
                        pg.check("#f-dimensions-province")
                        pg.wait_for_selector("#main svg.chart rect", timeout=10000)
                        pg.click(".seg >> text=Donut")
                        pg.wait_for_selector(".donutwrap", timeout=10000)
                        shot("explore_donut")
                        # command palette
                        pg.keyboard.press("Control+k")
                        pg.fill("#palette-q", "refund")
                        pg.wait_for_selector(".palette-item >> text=Refund rate", timeout=10000)
                        shot("palette")
                        pg.keyboard.press("Escape")
                        # notifications bell
                        pg.click("#bell")
                        pg.wait_for_selector("#notif-panel")
                        pg.keyboard.press("Escape")
                        pg.goto(f"{base}/app#/pipelines/1")
                        pg.wait_for_selector("text=Run now")
                        pg.click("text=Run now")
                        pg.wait_for_selector("#main >> text=Run success", timeout=30000)
                        shot("run_live")
                    ctx.close()
                features(b, base, shots, problems)
                b.close()
        finally:
            srv.terminate()
            srv.wait(timeout=10)
            logf.seek(0)
            out = logf.read()
        if "Traceback" in out:
            problems.append("server traceback:\n" + out[-3000:])
    if problems:
        print("UI SMOKE FAILED")
        print("\n".join(problems))
        if os.environ.get("GITHUB_ACTIONS"):  # also as annotations, readable on the run page without opening the log
            for p in problems[:10]:
                print("::warning title=UI smoke::" + p.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")[:2000])
        sys.exit(1)
    print("UI smoke passed: landing, setup, sign-in and %d routes in light desktop and dark mobile, plus SQL chart, cross-filtering, insights, explain-change, deep dive, forecast, plain-English questions, every visual, the dashboard designer, sharing and embedding, connector forms, the tour and the demo switch, Explore, search palette, notifications, quality run and live pipeline run." % len(routes))


if __name__ == "__main__":
    main()
