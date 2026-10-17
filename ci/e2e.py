"""CI-only end-to-end checks + screenshots for the paste-text feature.

Usage: python e2e.py <base_url> <logs_dir> <out_dir>
"""
import glob
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

from playwright.sync_api import sync_playwright

BASE, LOGS, OUT = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
os.makedirs(OUT, exist_ok=True)
failures = []
report = []


def shot(page, **kw):
    page.wait_for_timeout(450)  # let CSS transitions / animate-in settle
    page.screenshot(**kw)


def log(msg):
    print(msg, flush=True)
    report.append(msg)


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v + b"\r\n"
    for k, (name, data) in files.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                 f"Content-Type: text/plain\r\n\r\n").encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def post(path, body, ctype):
    req = urllib.request.Request(BASE + path, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def decode_like_browser(raw):
    """What a user gets on the clipboard after opening the file in an editor."""
    if raw[:2] == b"\xff\xfe":
        text = raw[2:].decode("utf-16-le", "replace")
    elif raw[:2] == b"\xfe\xff":
        text = raw[2:].decode("utf-16-be", "replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw[3:].decode("utf-8", "replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Legacy code pages (cp1250/1251/MacCentralEurope...): what an editor shows.
            from charset_normalizer import from_bytes
            text = str(from_bytes(raw).best())
    # Browsers submit textarea line breaks as CRLF.
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


# ---------------------------------------------------------------- parity
log("## File upload vs pasted text parity (POST /api)")
logs = sorted(glob.glob(os.path.join(LOGS, "**", "originals", "*"), recursive=True))
same = diff = 0
for path in logs:
    raw = open(path, "rb").read()
    if len(raw) > 200 * 1024:
        continue
    name = os.path.relpath(path, LOGS)
    b1, c1 = multipart({}, {"logfile": ("rip.log", raw)})
    s1, r1 = post("/api", b1, c1)
    b2, c2 = multipart({"logtext": decode_like_browser(raw).encode("utf-8")}, {})
    s2, r2 = post("/api", b2, c2)
    j1, j2 = json.loads(r1), json.loads(r2)
    keys = ("score", "checksum_state", "ripper", "language")
    a = {k: j1.get(k) for k in keys}
    b = {k: j2.get(k) for k in keys}
    if s1 == s2 == 200 and a == b:
        same += 1
    else:
        diff += 1
        log(f"- DIFF {name}: file={s1} {a} | paste={s2} {b} {j2.get('error','')}")
log(f"parity: {same} identical, {diff} different, of {same + diff} logs")

# ---------------------------------------------------------------- API errors
log("\n## API input validation")
sample = open(logs[0], "rb").read()
cases = [
    ("both fields", *multipart({"logtext": b"Exact Audio Copy"}, {"logfile": ("a.log", sample)}), 400, "not both"),
    ("neither", *multipart({}, {}), 400, "No log provided"),
    ("too large text", *multipart({"logtext": b"a" * (200 * 1024 + 1)}, {}), 413, "200 KB"),
    ("huge body", *multipart({"logtext": b"a" * (2 * 1024 * 1024)}, {}), 413, "200 KB"),
    ("markup", *multipart({"logtext": b'Exact Audio Copy\r\n<span class="good">x</span>'}, {}), 400, "HTML markup"),
    ("invalid utf-8", *multipart({"logtext": b"Exact Audio Copy \xff\xfe\xfd"}, {}), 400, "UTF-8"),
]
form = urllib.parse.urlencode({"logtext": decode_like_browser(sample)}).encode()
st, body = post("/api", form, "application/x-www-form-urlencoded")
log(f"- urlencoded logtext -> {st} score={json.loads(body).get('score')}")
if st != 200:
    failures.append("urlencoded logtext")
for label, body, ctype, want_status, want_msg in cases:
    st, resp = post("/api", body, ctype)
    ok = st == want_status and want_msg in resp.decode("utf-8", "replace")
    log(f"- {'ok ' if ok else 'FAIL'} {label}: {st} {resp.decode('utf-8', 'replace').strip()[:120]}")
    if not ok:
        failures.append(label)

# ---------------------------------------------------------------- browser
XSS = "\r\n".join([
    "Exact Audio Copy V1.6 from 23. October 2020",
    "",
    "EAC extraction logfile from 1. January 2024",
    "",
    "Used drive  : <img src=x onerror=\"window.__xss=1\">",
    "Read mode   : <script>window.__xss=2</script>",
    "Gap         : <svg/onload=window.__xss=3> &lt;img src=x onerror=window.__xss=4&gt;",
    "Album       : Simon & Garfunkel </pre><a href='//evil.example'>click</a>",
])

with sync_playwright() as p:
    browser = p.chromium.launch()
    for theme in ("dark", "light"):
        for vp_name, vp in (("desktop", {"width": 1280, "height": 900}), ("mobile", {"width": 390, "height": 844})):
            ctx = browser.new_context(viewport=vp, color_scheme=theme, device_scale_factor=2 if vp_name == "mobile" else 1)
            ctx.add_init_script(f"localStorage.setItem('logchecker-theme', '{theme}')")
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            tag = f"{theme}-{vp_name}"

            page.goto(BASE + "/")
            shot(page, path=f"{OUT}/{tag}-01-home-file.png", full_page=True)
            page.click("#tab-text")
            shot(page, path=f"{OUT}/{tag}-02-home-paste-empty.png", full_page=True)
            page.fill("#logtext", decode_like_browser(sample).replace("\r\n", "\n"))
            shot(page, path=f"{OUT}/{tag}-03-home-paste-filled.png", full_page=True)
            page.fill("#logtext", "x" * (200 * 1024 + 10))
            shot(page, path=f"{OUT}/{tag}-04-paste-too-large.png", full_page=True)
            if not page.is_disabled("#btn-analyze"):
                failures.append(f"{tag}: analyze not disabled when too large")

            # Submit the real sample via the paste box.
            page.fill("#logtext", decode_like_browser(sample).replace("\r\n", "\n"))
            page.click("#btn-analyze")
            page.wait_for_selector(".log-line", timeout=60000)
            shot(page, path=f"{OUT}/{tag}-05-result-from-paste.png", full_page=True)
            mode_after = page.get_attribute("#tab-text", "aria-selected")
            log(f"\n[{tag}] after paste submit: text tab selected={mode_after}, score={page.text_content('#score-number')}")

            # Error path: markup rejected, draft restored.
            page.click("#tab-text")
            page.fill("#logtext", "Exact Audio Copy\n<span class=\"good\">fake</span>")
            page.click("#btn-analyze")
            page.wait_for_selector("#error-alert")
            restored = page.input_value("#logtext")
            shot(page, path=f"{OUT}/{tag}-06-error-draft-restored.png", full_page=True)
            log(f"[{tag}] error: {page.text_content('#error-alert').strip()!r}; draft restored={'fake' in restored}")
            if "fake" not in restored:
                failures.append(f"{tag}: draft not restored")

            # Injection payloads that are NOT HTML-tag-shaped by our filter go through
            # the full pipeline; the rendered report must not execute or link them.
            page.goto(BASE + "/")
            page.click("#tab-text")
            page.fill("#logtext", XSS)
            page.click("#btn-analyze")
            page.wait_for_selector(".log-line", timeout=60000)
            page.fill("#log-search", "img")  # exercises the highlighter
            page.wait_for_timeout(300)
            page.fill("#log-search", "onerror")
            page.wait_for_timeout(300)
            page.click("#btn-expand")
            page.wait_for_timeout(300)
            shot(page, path=f"{OUT}/{tag}-07-xss-payload-rendered.png", full_page=False)
            page.keyboard.press("Escape")
            xss = page.evaluate("window.__xss || 0")
            live = page.evaluate("""() => document.querySelectorAll(
                '#output-container img, #output-container script, #output-container svg, #output-container a,'
                + '#log-modal-body img, #log-modal-body svg, #log-modal-body a').length""")
            log(f"[{tag}] xss executed={xss}, live injected elements={live}")
            if xss or live:
                failures.append(f"{tag}: injection rendered (xss={xss}, live={live})")

            # Global paste (outside a text field) fills the paste box.
            page.goto(BASE + "/")
            page.click("#tab-file")
            page.evaluate("""() => {
                const dt = new DataTransfer(); dt.setData('text/plain', 'Exact Audio Copy V1.6\\nline 2');
                document.body.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true}));
            }""")
            gp = page.input_value("#logtext")
            log(f"[{tag}] global paste -> text tab={page.get_attribute('#tab-text', 'aria-selected')}, filled={gp[:21]!r}")
            if not gp.startswith("Exact Audio Copy"):
                failures.append(f"{tag}: global paste")

            # Keyboard: arrow keys between tabs.
            page.focus("#tab-text")
            page.keyboard.press("ArrowLeft")
            if page.get_attribute("#tab-file", "aria-selected") != "true":
                failures.append(f"{tag}: arrow key tab switch")
            shot(page, path=f"{OUT}/{tag}-08-focus-file-tab.png")

            if errors:
                log(f"[{tag}] console errors: {errors}")
                failures.append(f"{tag}: console errors")
            ctx.close()

    # No-JS: form must be the original plain file upload.
    ctx = browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(BASE + "/")
    shot(page, path=f"{OUT}/nojs-home.png", full_page=True)
    vis = page.is_visible("#input-tabs"), page.is_visible("#paste-zone"), page.is_visible("#file-drop-zone")
    log(f"\n[no-js] tabs visible={vis[0]}, paste visible={vis[1]}, drop zone visible={vis[2]}")
    if vis != (False, False, True):
        failures.append("no-js layout")
    browser.close()

log("\n## Result: " + ("PASS" if not failures else "FAIL: " + ", ".join(failures)))
open(f"{OUT}/report.md", "w").write("\n".join(report))
sys.exit(1 if failures else 0)
