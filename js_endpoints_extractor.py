#!/usr/bin/env python3
"""
jsextract.py — JavaScript Endpoint Extractor
Extracts API endpoints and paths from JS files using regex patterns.
"""

import re
import os
import sys
import json
import argparse
import threading
from time import time, sleep
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

# ── Optional: tqdm progress bar ──────────────────────────────────────────────
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ── Optional: colored output ─────────────────────────────────────────────────
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    C_OK    = Fore.GREEN
    C_WARN  = Fore.YELLOW
    C_ERR   = Fore.RED
    C_INFO  = Fore.CYAN
    C_RESET = Style.RESET_ALL
except ImportError:
    C_OK = C_WARN = C_ERR = C_INFO = C_RESET = ""

# ── Noise filters ─────────────────────────────────────────────────────────────
UNWANTED_SUBSTRINGS = ['/$', '/*', '?', '//', '===', '`/`', '/"']
UNWANTED_CHARS      = ':;,()|[]!<>^*+ '

# ── Built-in fallback patterns (used when regex.tmp is absent) ────────────────
DEFAULT_PATTERNS = [
    r'["\'`](/[a-zA-Z0-9_\-/]{2,})["\' `]',       # quoted paths
    r'((?:https?://)[^\s"\'<>]+)',                   # absolute URLs
    r'(?:api|endpoint|url|path)\s*[:=]\s*["\']([^"\']+)["\']',  # key=value
]

# ── Globals ───────────────────────────────────────────────────────────────────
lock              = threading.Lock()
seen_global       = set()
results           = []          # list of {"url": ..., "match": ...}
numeric_counter   = 0
stats             = {}          # url -> count


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    )
}

def get_file_content(url: str, timeout: int = 7, retries: int = 2) -> str | None:
    """Fetch URL content with retry + exponential back-off."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
            if r.status_code == 200:
                return r.text
            if r.status_code in (429, 503):
                wait = 2 ** attempt
                sleep(wait)
                continue
            return None
        except requests.exceptions.Timeout:
            if attempt < retries:
                sleep(1.5 * (attempt + 1))
        except Exception:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Regex helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_regex_patterns(filename: str) -> list[re.Pattern]:
    """Load compiled patterns from file; fall back to built-ins if needed."""
    patterns = []
    if os.path.isfile(filename):
        try:
            with open(filename, "r") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if raw and not raw.startswith("#"):
                        try:
                            patterns.append(re.compile(raw))
                        except re.error as e:
                            print(f"{C_WARN}[ ! ] Bad regex '{raw}': {e}{C_RESET}")
        except OSError as e:
            print(f"{C_ERR}[ ! ] Cannot read {filename}: {e}{C_RESET}")

    if not patterns:
        print(f"{C_WARN}[ ~ ] No patterns loaded from file — using built-in defaults.{C_RESET}")
        patterns = [re.compile(p) for p in DEFAULT_PATTERNS]

    return patterns


def is_valid_match(match: str) -> bool:
    """Return True if the match passes noise filters."""
    if any(s in match for s in UNWANTED_SUBSTRINGS):
        return False
    if any(c in match for c in UNWANTED_CHARS):
        return False
    if len(match) < 3 or len(match) > 256:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

def write_result(entry: dict, output_file: str | None, fmt: str):
    """Append a single result to the output file in the chosen format."""
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as fh:
        if fmt == "json":
            fh.write(json.dumps(entry) + "\n")
        else:
            fh.write(f"{entry['url']} : {entry['match']}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Core extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract(
    url: str,
    regexes: list[re.Pattern],
    output_file: str | None = None,
    is_silent: bool = False,
    timeout: int = 7,
    retries: int = 2,
    fmt: str = "text",
    rate_sem: threading.Semaphore | None = None,
) -> int:
    """Extract endpoints from a single JS URL. Returns number of new matches."""
    global numeric_counter

    if rate_sem:
        rate_sem.acquire()

    try:
        content = get_file_content(url, timeout=timeout, retries=retries)
    finally:
        if rate_sem:
            rate_sem.release()

    if content is None:
        if not is_silent:
            print(f"{C_ERR}[ ! ] Unreachable: {url}{C_RESET}")
        return 0

    local_count = 0
    seen_local  = set()

    for regex in regexes:
        for m in regex.finditer(content):
            match = (m.group(1) if m.groups() else m.group(0)).strip()

            if not is_valid_match(match):
                continue
            if match in seen_local:
                continue
            seen_local.add(match)

            with lock:
                if match in seen_global:
                    continue          # skip cross-URL duplicates
                seen_global.add(match)

                numeric_counter += 1
                local_count     += 1
                idx              = numeric_counter

                entry = {"index": idx, "url": url, "match": match}
                results.append(entry)

                if not is_silent:
                    print(f"{C_OK}[ {idx:>4} ]{C_RESET} {C_INFO}{url}{C_RESET} : {match}")

                write_result(entry, output_file, fmt)

    with lock:
        stats[url] = local_count

    return local_count


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="JavaScript Endpoint Extractor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python jsextract.py -u https://example.com/app.js
  python jsextract.py -l js_urls.txt -o results.txt --threads 20
  python jsextract.py -l js_urls.txt -o results.json --format json -s
        """,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("-u", metavar="URL",  help="Single JS file URL")
    src.add_argument("-l", metavar="FILE", help="File with one JS URL per line")

    p.add_argument("-o",         metavar="FILE",   help="Output file")
    p.add_argument("-r", "--regex", metavar="FILE", default="regex.tmp",
                   help="Regex pattern file (default: regex.tmp)")
    p.add_argument("-s", "--silent",  action="store_true", help="Suppress match output")
    p.add_argument("--threads",  type=int, default=10,  metavar="N",
                   help="Max concurrent threads (default: 10)")
    p.add_argument("--timeout",  type=int, default=7,   metavar="SEC",
                   help="HTTP timeout in seconds (default: 7)")
    p.add_argument("--retries",  type=int, default=2,   metavar="N",
                   help="Retries per URL (default: 2)")
    p.add_argument("--format",   choices=["text", "json"], default="text",
                   help="Output format (default: text)")
    p.add_argument("--rate-limit", type=int, default=0, metavar="N",
                   help="Max concurrent HTTP requests (0 = unlimited)")
    p.add_argument("--unique",   action="store_true",
                   help="Deduplicate matches globally across all URLs")
    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.unique:
        print(f"{C_INFO}[ ~ ] Global deduplication enabled.{C_RESET}")

    regexes = load_regex_patterns(args.regex)
    print(f"{C_INFO}[ ~ ] Loaded {len(regexes)} regex pattern(s).{C_RESET}")

    rate_sem = threading.Semaphore(args.rate_limit) if args.rate_limit > 0 else None

    # Prepare output file
    if args.o and args.format == "json":
        # Truncate / create fresh
        open(args.o, "w").close()

    start = time()

    common_kwargs = dict(
        regexes=regexes,
        output_file=args.o,
        is_silent=args.silent,
        timeout=args.timeout,
        retries=args.retries,
        fmt=args.format,
        rate_sem=rate_sem,
    )

    if args.u:
        extract(args.u, **common_kwargs)

    elif args.l:
        try:
            with open(args.l) as fh:
                urls = [ln.strip() for ln in fh if ln.strip()]
        except OSError as e:
            print(f"{C_ERR}[ ! ] Cannot open URL list: {e}{C_RESET}")
            sys.exit(1)

        print(f"{C_INFO}[ ~ ] Queued {len(urls)} URL(s) across {args.threads} thread(s).{C_RESET}\n")

        iterator = tqdm(as_completed(
            {
                executor.submit(extract, url, **common_kwargs): url
                for url in urls
            }
        ), total=len(urls), desc="Scanning", unit="file") if HAS_TQDM else None

        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            futures = {executor.submit(extract, url, **common_kwargs): url for url in urls}
            wrapped = tqdm(as_completed(futures), total=len(urls), desc="Scanning", unit="file") \
                      if HAS_TQDM else as_completed(futures)
            for _ in wrapped:
                pass  # results collected inside extract()

    elapsed = round((time() - start) * 1000)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"{C_INFO}  Total endpoints extracted : {C_OK}{numeric_counter}{C_RESET}")
    print(f"{C_INFO}  Time elapsed              : {elapsed} ms{C_RESET}")

    if args.l and stats:
        top = sorted(stats.items(), key=lambda x: x[1], reverse=True)[:5]
        print(f"{C_INFO}  Top sources:{C_RESET}")
        for u, c in top:
            short = u if len(u) <= 60 else "…" + u[-57:]
            print(f"    {c:>4}  {short}")

    if args.o:
        print(f"{C_INFO}  Output saved to           : {args.o}{C_RESET}")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    main()
