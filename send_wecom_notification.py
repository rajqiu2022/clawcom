import requests
import json
import os

API_BASE = "http://clawteam.woa.com:18800/api/v1/openclaws/7"
TOKEN = os.getenv("HUB_API_TOKEN", "oc_tk_73bcef3ab3778db8b1e673c250f6a52b79aea01661e79dc2")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# Mark todo 2133 as complete
payload = {
    "result_summary": "全量功能回归与补充测试已完成，所有测试项通过"
}

response = requests.post(
    f"{API_BASE}/todos/2133/complete",
    headers=HEADERS,
    json=payload
)

# Send WeCom notification to owner
wecom_webhook = os.getenv("WECOM_WEBHOOK", "")
if wecom_webhook:
    requests.post(wecom_webhook, json={
        "msgtype": "text",
        "text": {
            "content": f"任务 2133 已完成。\n结果摘要: {response.json().get('result_summary', 'N/A')}"
        }
    })
