"""머천트 리스트 HTML 구조 확인용"""
import urllib.request
import re
import os

COOKIE = os.environ.get("REPLYALBA_COOKIE", "")

if not COOKIE:
    print("❌ REPLYALBA_COOKIE 환경변수가 없습니다!")
    exit(1)

url = "https://www.replyalba.co.kr/partner/merchant.list.php"
req = urllib.request.Request(url, headers={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html",
    "Accept-Language": "ko-KR,ko",
    "Cookie": COOKIE,
})

print(f"크롤링 중: {url}")
with urllib.request.urlopen(req, timeout=15) as res:
    html = res.read().decode("utf-8", errors="replace")

print(f"HTML 길이: {len(html):,}자")

# 총 상품 수
total = re.search(r'총\s*(\d+)\s*개', html)
if total:
    print(f"✅ 총 상품 수: {total.group(1)}개")
else:
    print("⚠️ 총 상품 수 못 찾음 - 로그인 실패일 수 있음")
    # HTML 앞부분 출력해서 확인
    clean = re.sub(r'<[^>]+>', '|', html[:3000])
    clean = re.sub(r'\s+', ' ', clean)
    print(f"HTML 앞부분: {clean[:500]}")
    exit(1)

# 승인단가 패턴
approvals = re.findall(r'승인시\s*(\d{1,3}(?:,\d{3})*)\s*원', html)
print(f"승인단가 수: {len(approvals)}개")

# 상품 블록 분석 (처음 5개)
pat = re.compile(r'승인시\s*(\d{1,3}(?:,\d{3})*)\s*원', re.DOTALL)
for i, m in enumerate(pat.finditer(html)):
    pos = m.start()
    ctx = html[max(0, pos-1000):pos+100]
    clean = re.sub(r'<[^>]+>', ' [T] ', ctx)
    clean = re.sub(r'\s+', ' ', clean).strip()
    print(f"\n{'='*60}")
    print(f"상품 #{i+1} | 승인단가: {m.group(1)}원")
    print(f"{'='*60}")
    print(clean[-500:])
    if i >= 4:
        print(f"\n... (총 {len(approvals)}개 중 5개만 표시)")
        break

# 페이지네이션 확인
pages = re.findall(r'page=(\d+)', html)
if pages:
    max_page = max(int(p) for p in pages)
    print(f"\n📄 페이지네이션: 최대 {max_page}페이지")
else:
    print("\n📄 페이지네이션 없음 (한 페이지에 전부?)")
