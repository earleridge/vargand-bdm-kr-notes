
import os
import json
import calendar
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

now = datetime.now(ZoneInfo("Asia/Manila"))
last_day = calendar.monthrange(now.year, now.month)[1]

is_last_day = now.day == last_day
is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

if not is_last_day and not is_manual_run:
    print(f"Today is not month-end ({now:%Y-%m-%d}). Skipping.")
    raise SystemExit(0)

message = """@everyone,
🗓️ **Monthly Reset Check** 🗓️
- Desert Temples
- Shakatu Shop 1 & 2
"""

payload = {
    "content": message,
    "allowed_mentions": {
        "parse": ["everyone"]
    }
}

data = json.dumps(payload).encode("utf-8")

request = urllib.request.Request(
    WEBHOOK_URL,
    data=data,
    headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; BDMMonthlyReset/1.0)"
    },
    method="POST"
)

with urllib.request.urlopen(request, timeout=30) as response:
    print(f"Monthly Reset notification sent. HTTP status: {response.status}")
