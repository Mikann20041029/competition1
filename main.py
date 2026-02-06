import os
import json
import re
import time
import hashlib
import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from openai import OpenAI

CONFIG_PATH = "config.json"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise RuntimeError("Missing env: DEEPSEEK_API_KEY")

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

UA = "Mozilla/5.0 (compatible; AutoContestBot/1.0; +https://github.com/)"

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def safe_get(url: str, timeout=30) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    return r.text

def normalize_url(u: str) -> str:
    u = u.strip()
    if not u:
        return ""
    # drop fragments
    parsed = urlparse(u)
    return parsed._replace(fragment="").geturl()

def looks_like_candidate(text: str, hints: list[str]) -> bool:
    t = (text or "").lower()
    score = 0
    for h in hints:
        if h.lower() in t:
            score += 1
    return score >= 1

def extract_links(base_url: str, html: str, hints: list[str]) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        label = (a.get_text(" ", strip=True) or "").strip()
        if not href:
            continue
        abs_url = normalize_url(urljoin(base_url, href))
        if not abs_url.startswith("http"):
            continue
        # filter obvious junk
        if any(x in abs_url.lower() for x in ["javascript:", "mailto:"]):
            continue
        blob = f"{label} {abs_url}"
        if looks_like_candidate(blob, hints):
            out.append({"url": abs_url, "label": label})
    # de-dupe by url
    seen = set()
    uniq = []
    for x in out:
        if x["url"] in seen:
            continue
        seen.add(x["url"])
        uniq.append(x)
    return uniq

def fetch_page_excerpt(url: str, max_chars=8000) -> str:
    html = safe_get(url)
    soup = BeautifulSoup(html, "html.parser")
    # remove scripts/styles
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # compress whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]

def deepseek_structurize(url: str, label: str, page_text: str) -> dict:
    prompt = f"""
You extract contest/campaign/call-for-entry information from a webpage.

Return STRICT JSON only (no markdown, no commentary).
If unknown, use null or empty string. Do NOT invent facts.

Fields:
- title (string)
- organizer (string)
- reward (string)  // prize/money/gift
- deadline (string) // ISO date if possible, else raw text
- eligibility (string)
- required_submission (string)
- submission_format (string) // file type / size / word count etc
- how_to_submit (string)
- submission_url (string) // the form/entry page url (best guess)
- notes (string)
- confidence (number 0-1)

Input:
- source_url: {url}
- link_label: {label}

Page text:
{page_text}
""".strip()

    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    raw = resp.choices[0].message.content or "{}"
    # try parse json robustly
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # attempt to salvage first {...}
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {"title": label or url, "submission_url": url, "notes": "JSON parse failed", "confidence": 0.0}
        data = json.loads(m.group(0))

    # normalize submission_url
    su = (data.get("submission_url") or "").strip()
    data["submission_url"] = su if su.startswith("http") else url
    data["_source_url"] = url
    data["_link_label"] = label
    return data

def parse_deadline(deadline_str: str):
    if not deadline_str:
        return None
    s = deadline_str.strip()
    # already ISO
    try:
        dt = dtparser.parse(s, fuzzy=True)
        return dt
    except Exception:
        return None

def score_item(item: dict) -> float:
    # You want: easy submit + decent reward + near-ish deadline + high confidence
    conf = float(item.get("confidence") or 0.0)
    reward = (item.get("reward") or "").lower()
    required = (item.get("required_submission") or "").lower()
    how = (item.get("how_to_submit") or "").lower()

    # heuristic
    s = 0.0
    s += conf * 2.0

    if any(x in reward for x in ["万円", "yen", "¥", "gift", "ギフト", "商品券", "amazon"]):
        s += 1.0

    # prefer lighter submissions
    if any(x in required for x in ["short", "短文", "キャッチ", "アイデア", "tweet", "コメント", "写真"]):
        s += 1.0
    if any(x in required for x in ["essay", "長文", "research", "開発", "prototype", "動画", "portfolio"]):
        s -= 0.8

    if any(x in how for x in ["form", "フォーム", "web", "online", "google form", "応募フォーム"]):
        s += 0.8

    # deadline weighting: nearer is better but not overdue
    dt = parse_deadline(item.get("deadline") or "")
    if dt:
        now = datetime.datetime.now(dt.tzinfo or datetime.timezone.utc)
        delta_days = (dt - now).total_seconds() / 86400.0
        if delta_days < -1:
            s -= 2.0
        elif delta_days < 7:
            s += 0.6
        elif delta_days < 30:
            s += 0.4
        else:
            s += 0.1

    return s

def md_escape(s: str) -> str:
    return (s or "").replace("\r", "").strip()

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def main():
    cfg = load_config()
    sources = cfg["sources"]
    hints = cfg.get("keyword_hints", [])
    max_candidates = int(cfg.get("max_candidates", 40))
    out_md = cfg.get("output_markdown", "output/weekly_cards.md")
    out_dir = cfg.get("output_dir", "output")

    ensure_dir(out_dir)

    # 1) collect candidates
    candidates = []
    for src in sources:
        try:
            html = safe_get(src)
            links = extract_links(src, html, hints)
            candidates.extend(links)
            time.sleep(1.0)
        except Exception as e:
            candidates.append({"url": src, "label": f"[source fetch failed] {e}"})

    # de-dupe
    seen = set()
    uniq = []
    for c in candidates:
        u = normalize_url(c["url"])
        if not u or u in seen:
            continue
        seen.add(u)
        uniq.append({"url": u, "label": c.get("label","")})
    candidates = uniq[:max_candidates]

    # 2) structurize via DeepSeek
    items = []
    for i, c in enumerate(candidates, 1):
        url = c["url"]
        label = c.get("label", "")
        try:
            page_text = fetch_page_excerpt(url)
            data = deepseek_structurize(url, label, page_text)
            data["_score"] = score_item(data)
            items.append(data)
        except Exception as e:
            items.append({
                "title": label or url,
                "_source_url": url,
                "submission_url": url,
                "notes": f"failed: {e}",
                "confidence": 0.0,
                "_score": -99.0
            })
        time.sleep(1.2)  # be gentle

    # 3) rank
    items.sort(key=lambda x: x.get("_score", 0.0), reverse=True)

    # 4) write markdown
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    lines = []
    lines.append(f"# Weekly Submission List ({now})")
    lines.append("")
    lines.append("※最終提出は手動でOK。ここに「提出URL」「要件」「締切」がまとまって出る。")
    lines.append("")

    for idx, it in enumerate(items[:30], 1):
        title = md_escape(it.get("title") or it.get("_link_label") or it.get("_source_url"))
        sub_url = md_escape(it.get("submission_url") or it.get("_source_url"))
        src_url = md_escape(it.get("_source_url") or "")
        lines.append(f"## {idx}. {title}")
        lines.append(f"- Score: {it.get('_score',0):.2f} / Confidence: {it.get('confidence',0)}")
        lines.append(f"- Submission URL: {sub_url}")
        if src_url and src_url != sub_url:
            lines.append(f"- Source URL: {src_url}")
        lines.append(f"- Deadline: {md_escape(it.get('deadline',''))}")
        lines.append(f"- Reward: {md_escape(it.get('reward',''))}")
        lines.append(f"- Eligibility: {md_escape(it.get('eligibility',''))}")
        lines.append(f"- Required submission: {md_escape(it.get('required_submission',''))}")
        lines.append(f"- Submission format: {md_escape(it.get('submission_format',''))}")
        lines.append(f"- How to submit: {md_escape(it.get('how_to_submit',''))}")
        notes = md_escape(it.get("notes",""))
        if notes:
            lines.append(f"- Notes: {notes}")
        lines.append("")

    ensure_dir(os.path.dirname(out_md) or ".")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[OK] wrote: {out_md}")

if __name__ == "__main__":
    main()
