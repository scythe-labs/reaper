import difflib
import re
import subprocess

FILES = ["00-tokens", "04-buttons", "22-queue-filters", "29-setup", "34-lists"]


def strip(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [l for l in text.splitlines() if l.strip()]


def show(rev: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True).stdout


tot_a = tot_r = 0
raw_a = raw_r = 0
for f in FILES:
    p = f"frontend/src/styles/{f}.css"
    old = show("e4b13a0^", p)
    new = show("e4b13a0", p)
    a = strip(old)
    b = strip(new)
    d = list(difflib.unified_diff(a, b, lineterm=""))
    add = sum(1 for l in d if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in d if l.startswith("-") and not l.startswith("---"))
    print(f, f"+{add}/-{rem}", "net", add - rem)
    tot_a += add
    tot_r += rem
    d2 = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm=""))
    raw_a += sum(1 for l in d2 if l.startswith("+") and not l.startswith("+++"))
    raw_r += sum(1 for l in d2 if l.startswith("-") and not l.startswith("---"))
print("code-only TOTAL", f"+{tot_a}/-{tot_r}", "net", tot_a - tot_r)
print("raw TOTAL", f"+{raw_a}/-{raw_r}", "net", raw_a - raw_r)
