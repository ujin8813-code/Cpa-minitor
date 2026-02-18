import urllib.request
import re

url = "https://www.replyalba.com/mobile/promotion/pro.php?page=1"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
    "Accept": "text/html",
})

with urllib.request.urlopen(req, timeout=15) as res:
    html = res.read().decode("utf-8", errors="replace")

# 승인 단가 주변만 출력
pat = re.compile(r'승인시?\s*(?:</[^>]+>\s*)*(?:<[^>]+>\s*)*(\d{1,3}(?:,\d{3})*)\s*원', re.DOTALL)

for i, m in enumerate(pat.finditer(html)):
    pos = m.start()
    ctx = html[max(0,pos-500):pos+200]
    clean = re.sub(r'<[^>]+>', '|', ctx)
    clean = re.sub(r'\s+', ' ', clean).strip()
    print(f"\n=== 상품#{i+1} 승인:{m.group(1)}원 ===")
    print(clean[:400])
    if i >= 3:
        break
