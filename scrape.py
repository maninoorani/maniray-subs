#!/usr/bin/env python3
"""Collects v2ray configs posted as text in public Telegram channels,
tests them, and keeps only the working ones in tg_configs.txt."""
import base64, html as H, json, os, re, socket, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PAGES = int(os.environ.get("PAGES", 5))            # how many pages (about 20 posts each) to read per channel
MAX_FAILS = int(os.environ.get("MAX_FAILS", 2))    # failed runs in a row before a config is forgotten
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", 5))
MAX_TOTAL = int(os.environ.get("MAX_TOTAL", 800))
SCHEMES = "vless|vmess|trojan|ss|hysteria2|hy2|hysteria|tuic|anytls|wireguard"
LINK_RE = re.compile(r"(?:%s)://[^\s<>\"'`]+" % SCHEMES, re.I)
UDP = {"wireguard", "hysteria2", "hy2", "hysteria", "tuic"}


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def channel_names(path):
    names = []
    if not os.path.exists(path):
        return names
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.search(r"(?:t\.me/(?:s/)?|^@)?([A-Za-z][A-Za-z0-9_]{3,})/?(?:\?.*)?$", line)
        if m and m.group(1).lower() not in names:
            names.append(m.group(1).lower())
    return names


def clean(link):
    link = link.strip().rstrip(".,;:!)]}»")
    return link


def scrape_channel(name):
    found, before = [], None
    for _ in range(PAGES):
        url = "https://t.me/s/%s" % name + ("?before=%d" % before if before else "")
        page = fetch(url)
        ids = [int(x) for x in re.findall(r'data-post="[^"/]+/(\d+)"', page)]
        text = H.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", "\n", page)))
        found += [clean(x) for x in LINK_RE.findall(text)]
        if not ids:
            break
        low = min(ids)
        if before is not None and low >= before:
            break
        before = low
        time.sleep(1)
    return found


def b64(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "ignore")


def parse(link):
    """Returns (key, proto, host, port) or None."""
    m = re.match(r"^([a-zA-Z0-9]+)://", link)
    if not m:
        return None
    proto = m.group(1).lower()
    try:
        if proto == "vmess":
            j = json.loads(b64(link[8:]))
            return ("vmess|%s|%s|%s" % (j["add"], j["port"], j.get("id", "")), proto, j["add"], int(j["port"]))
        if proto == "ss":
            body = link[5:].split("#")[0].split("?")[0]
            if "@" not in body:
                body = b64(body)
            else:
                cred, _, hp = body.rpartition("@")
                if not re.match(r"^[\w+\-]+:", cred):
                    try:
                        cred = b64(cred)
                    except Exception:
                        pass
                body = cred + "@" + hp
            cred, _, hp = body.rpartition("@")
            host, _, port = hp.rpartition(":")
            return ("ss|%s|%s|%s" % (host, port, cred), proto, host, int(port))
        u = urlparse(link)
        if not u.hostname or not u.port:
            return None
        return ("%s|%s|%s|%s|%s" % (proto, u.hostname, u.port, u.username or "", u.query), proto, u.hostname, u.port)
    except Exception:
        return None


def tcp_ok(host, port, timeout=5):
    for _ in range(2):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            time.sleep(1)
    return False


def main():
    names = channel_names(os.path.join(HERE, "channels.txt"))
    state_path = os.path.join(HERE, "tg_state.json")
    out_path = os.path.join(HERE, "tg_configs.txt")
    try:
        state = json.load(open(state_path, encoding="utf-8"))
    except Exception:
        state = {}
    now = int(time.time())
    new = 0
    for n in names:
        try:
            links = scrape_channel(n)
        except Exception as e:
            print("channel %s failed: %s" % (n, e))
            continue
        print("channel %s: %d links" % (n, len(links)))
        for l in links:
            p = parse(l)
            if not p or p[0] in state:
                continue
            state[p[0]] = {"link": l, "proto": p[1], "host": p[2], "port": p[3], "first": now, "fails": 0}
            new += 1
    items = list(state.items())
    with ThreadPoolExecutor(max_workers=80) as ex:
        results = list(ex.map(lambda kv: True if kv[1]["proto"] in UDP else tcp_ok(kv[1]["host"], kv[1]["port"]), items))
    tested = sum(1 for kv in items if kv[1]["proto"] not in UDP)
    passed = sum(1 for kv, ok in zip(items, results) if ok and kv[1]["proto"] not in UDP)
    outage = tested > 30 and passed < tested * 0.2     # the runner itself probably has no network
    if outage:
        print("most tests failed (%d/%d); assuming a network problem, keeping the previous list" % (passed, tested))
    else:
        for (k, v), ok in zip(items, results):
            v["fails"] = 0 if ok else v["fails"] + 1
    for k, v in list(state.items()):
        if v["fails"] >= MAX_FAILS or now - v["first"] > MAX_AGE_DAYS * 86400:
            del state[k]
    if not outage:
        good = sorted((v for v in state.values() if v["fails"] == 0), key=lambda v: -v["first"])[:MAX_TOTAL]
        open(out_path, "w", encoding="utf-8").write("\n".join(v["link"] for v in good) + ("\n" if good else ""))
        print("kept %d working configs (%d new this run)" % (len(good), new))
    keep = dict(sorted(state.items(), key=lambda kv: -kv[1]["first"])[: MAX_TOTAL * 2])
    json.dump(keep, open(state_path, "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()
