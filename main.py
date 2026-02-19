"""
CPA 상품 모니터링 시스템 v4.0

리플알바 머천트 리스트 전체(387개+)를 크롤링하여
고단가 상품을 자동으로 찾아 디스코드로 알림을 보냅니다.

사용법:
    python main.py                  → Mock 데이터 테스트 (화면 출력)
    python main.py --live           → 실제 크롤링 (화면 출력)
    python main.py --live --discord → 실제 크롤링 + 디스코드 알림
    python main.py --test-discord   → 디스코드 연결 테스트
"""

import json
import os
import re
import sys
import subprocess
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path


# ============================================================
# 설정
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "여기에_웹훅_URL")
MIN_SCORE = 50.0            # 이 점수 이상만 추천 (0~100)
MIN_APPROVAL_PRICE = 30000  # 이 금액 이상만 대상 (원)
DATA_FILE = "data/products.json"


# ============================================================
# 상품 모델
# ============================================================
class Category(Enum):
    WEDDING = "결혼"
    DIET = "다이어트"
    INSURANCE = "보험"
    RECOVERY = "개인회생"
    HEALTH = "건기식"
    FINANCE = "재테크"
    LEGAL = "법률"
    LOTTO = "로또"
    RENTAL = "렌트/리스"
    LIFE = "라이프"
    OTHER = "기타"


CATEGORY_MAP = {
    "결혼": Category.WEDDING,
    "건강/다이어트": Category.DIET,
    "건강": Category.HEALTH,
    "다이어트": Category.DIET,
    "보험/상조": Category.INSURANCE,
    "보험": Category.INSURANCE,
    "상조": Category.INSURANCE,
    "개인회생/법률": Category.RECOVERY,
    "개인회생": Category.RECOVERY,
    "법률": Category.LEGAL,
    "재테크": Category.FINANCE,
    "1금융/대출": Category.FINANCE,
    "로또": Category.LOTTO,
    "렌트/리스": Category.RENTAL,
    "라이프": Category.LIFE,
    "광고/회원가입": Category.OTHER,
}

CATEGORY_WEIGHTS = {
    Category.WEDDING: 1.0, Category.RECOVERY: 0.9,
    Category.INSURANCE: 0.85, Category.LEGAL: 0.85,
    Category.FINANCE: 0.8, Category.RENTAL: 0.75,
    Category.HEALTH: 0.7, Category.DIET: 0.65,
    Category.LIFE: 0.6, Category.LOTTO: 0.4,
    Category.OTHER: 0.5,
}


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    merchant: str
    category: Category
    category_raw: str
    approval_price: int
    request_rate: float      # 전월 신청률
    approval_rate: float     # 전월 승인율
    has_promotion: bool
    detail_url: str
    source: str = "replyalba"

    @property
    def is_active(self):
        return True  # 머천트 리스트에 있으면 활성

    @property
    def effective_score_boost(self):
        """승인율이 높으면 보너스"""
        return min(self.approval_rate / 100, 1.0) * 0.2


# ============================================================
# 분석기
# ============================================================
@dataclass
class ScoredProduct:
    product: Product
    score: float
    reasons: list


class ProductAnalyzer:
    def __init__(self, min_score=50.0, min_price=30000):
        self.min_score = min_score
        self.min_price = min_price

    def analyze(self, products):
        scored = [self._score(p) for p in products]
        recommended = [s for s in scored if s.score >= self.min_score]
        return sorted(recommended, key=lambda s: s.score, reverse=True)

    def _score(self, p):
        reasons = []
        score = 0.0

        # 승인단가 (35점)
        price_score = min(p.approval_price / 200000, 1.0) * 35
        score += price_score
        if p.approval_price >= self.min_price:
            reasons.append(f"💰 고단가 {p.approval_price:,}원")

        # 승인율 (25점) — 실제 돈이 되려면 승인이 잘 돼야 함
        approval_score = min(p.approval_rate / 100, 1.0) * 25
        score += approval_score
        if p.approval_rate >= 80:
            reasons.append(f"✅ 승인율 {p.approval_rate}%")

        # 신청률 (15점) — 전환이 잘 되는 상품
        request_score = min(p.request_rate / 10, 1.0) * 15
        score += request_score
        if p.request_rate >= 3:
            reasons.append(f"📈 신청률 {p.request_rate}%")

        # 프로모션 (15점)
        if p.has_promotion:
            score += 15
            reasons.append("🔥 프로모션 진행중")

        # 카테고리 (10점)
        cat_weight = CATEGORY_WEIGHTS.get(p.category, 0.5)
        cat_score = cat_weight * 10
        score += cat_score
        if cat_weight >= 0.8:
            reasons.append(f"📌 {p.category_raw} (인기)")

        return ScoredProduct(product=p, score=round(score, 1), reasons=reasons)


# ============================================================
# 저장소
# ============================================================
class JsonRepository:
    def __init__(self, path=DATA_FILE):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def save_products(self, products):
        existing = self._load()
        for p in products:
            existing[p.id] = {
                "id": p.id, "name": p.name, "category": p.category_raw,
                "approval_price": p.approval_price,
                "request_rate": p.request_rate,
                "approval_rate": p.approval_rate,
                "has_promotion": p.has_promotion,
                "detail_url": p.detail_url,
                "updated": datetime.now().isoformat(),
            }
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)

    def get_known_ids(self):
        return set(self._load().keys())

    def _load(self):
        if not self._path.exists():
            return {}
        with open(self._path, "r", encoding="utf-8") as f:
            return json.load(f)


# ============================================================
# 디스코드 알림봇
# ============================================================
class DiscordNotifier:
    def __init__(self, webhook_url):
        self._url = webhook_url

    def notify(self, scored_products):
        if not scored_products:
            return

        lines = []
        lines.append(f"**🚨 CPA 추천 상품 {len(scored_products)}개 발견!**")
        lines.append("")

        for i, item in enumerate(scored_products[:10], 1):
            p = item.product
            icon = "🔥" if item.score >= 70 else "✅"
            promo = " 🎁" if p.has_promotion else ""
            lines.append(f"{icon} **[{i}] {p.name}**{promo}")
            lines.append(f"   승인단가: **{p.approval_price:,}원** | 점수: {item.score}점")
            lines.append(f"   카테고리: {p.category_raw}")
            lines.append(f"   승인율: {p.approval_rate}% | 신청률: {p.request_rate}%")
            if item.reasons:
                lines.append(f"   {' | '.join(item.reasons)}")
            lines.append("")

        if len(scored_products) > 10:
            lines.append(f"... 외 {len(scored_products) - 10}개 더")

        message = "\n".join(lines)
        if len(message) > 1900:
            message = message[:1900] + "\n... (더 많은 상품이 있습니다)"

        self._send({"content": message})

    def send_test(self):
        try:
            self._send({"content": "🔔 CPA 모니터 v4 연결 성공!"})
            return True
        except Exception as e:
            print(f"디스코드 에러: {e}")
            return False

    def _send(self, payload):
        data = json.dumps(payload)
        print(f"   디스코드 전송 중... ({len(data)}자)")
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", "-H", "Content-Type: application/json",
             "-d", data, self._url],
            capture_output=True, text=True, timeout=15,
        )
        code = result.stdout.strip()
        print(f"   디스코드 응답: {code}")
        if code not in ("200", "204"):
            raise Exception(f"Discord error: {code}")


# ============================================================
# 콘솔 알림
# ============================================================
class ConsoleNotifier:
    def notify(self, scored_products):
        if not scored_products:
            print("\n📭 추천할 신규 상품이 없습니다.\n")
            return
        print(f"\n{'='*60}")
        print(f"🚨 추천 상품 {len(scored_products)}개 발견!")
        print(f"{'='*60}")
        for i, item in enumerate(scored_products, 1):
            p = item.product
            icon = "🔥" if item.score >= 70 else "✅"
            promo = " [프로모션]" if p.has_promotion else ""
            print(f"\n{icon} [{i}] {p.name}{promo}")
            print(f"   점수: {item.score}점 | 승인단가: {p.approval_price:,}원")
            print(f"   카테고리: {p.category_raw}")
            print(f"   승인율: {p.approval_rate}% | 신청률: {p.request_rate}%")
            if item.reasons:
                print(f"   이유: {' | '.join(item.reasons)}")
        print(f"\n{'='*60}\n")


# ============================================================
# 리플알바 크롤러 v4 — 머천트 리스트 전체 크롤링
# ============================================================
class ReplyAlbaScraper:
    """
    머천트 리스트 페이지 (merchant.list.php) 기반 크롤링
    
    HTML 구조:
    <div class="contents_list_box">
      <div class="thumb_title">
        카테고리명<br/>
        <span><a href="./merchant.detail.php?idx=XXX">상품명</a></span>
      </div>
      <div class="thumb_sub_title">
        전월신청률 : X.XX%&nbsp;/&nbsp;전월승인율 : XX.XX%
      </div>
      <div class="thumb_sum">
        <span class="comm_msg">승인시</span> XX,XXX원
      </div>
      <div class="thumb_sum_event_red">프로모션</div>  ← 있으면 프로모션
    </div>
    """
    BASE_URL = "https://www.replyalba.co.kr/partner/merchant.list.php"

    def __init__(self, max_pages=20):
        self.max_pages = max_pages

    def fetch_products(self):
        all_products = []
        seen = set()

        for page in range(1, self.max_pages + 1):
            try:
                print(f"   📄 {page}페이지 크롤링 중...")
                html = self._fetch(page)
                products = self._parse(html)

                if not products:
                    print(f"   → 더 이상 상품 없음. 종료.")
                    break

                for p in products:
                    if p.id not in seen:
                        all_products.append(p)
                        seen.add(p.id)

                print(f"   → {len(products)}개 수집 (누적: {len(all_products)}개)")
            except Exception as e:
                print(f"   ❌ {page}페이지 실패: {e}")
                break

        print(f"   ✅ 총 {len(all_products)}개 수집 완료")
        return all_products

    def _fetch(self, page):
        url = f"{self.BASE_URL}?cate=&sort=app&search=&page={page}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html",
            "Accept-Language": "ko-KR,ko",
        })
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.read().decode("utf-8", errors="replace")

    def _parse(self, html):
        products = []

        # 각 상품 블록: <div class="contents_list_box"> ... </div>
        blocks = re.findall(
            r'<div class="contents_list_box">(.*?)</div>\s*</div>\s*</div>',
            html, re.DOTALL
        )

        # 블록이 안 잡히면 더 넓은 패턴 시도
        if not blocks:
            blocks = html.split('class="contents_list_box"')[1:]

        for block in blocks:
            try:
                product = self._parse_block(block)
                if product:
                    products.append(product)
            except Exception as e:
                continue

        return products

    def _parse_block(self, block):
        # 상품 ID (idx)
        idx_match = re.search(r'merchant\.detail\.php\?idx=(\d+)', block)
        if not idx_match:
            return None
        idx = idx_match.group(1)
        pid = f"ra-{idx}"
        detail_url = f"https://www.replyalba.co.kr/partner/merchant.detail.php?idx={idx}"

        # 카테고리 + 상품명
        # <div class="thumb_title"> 카테고리 <br/> <span><a>상품명</a></span>
        title_match = re.search(
            r'class="thumb_title">\s*(.*?)\s*<br\s*/?\s*>\s*<span><a[^>]*>\s*(.*?)\s*</a></span>',
            block, re.DOTALL
        )
        if title_match:
            category_raw = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
            name = re.sub(r'<[^>]+>', '', title_match.group(2)).strip()
        else:
            return None

        # 카테고리 매핑
        category = CATEGORY_MAP.get(category_raw, Category.OTHER)

        # 승인단가
        price_match = re.search(r'승인시.*?(\d{1,3}(?:,\d{3})*)\s*원', block, re.DOTALL)
        approval_price = int(price_match.group(1).replace(",", "")) if price_match else 0

        # 전월 신청률 / 승인율
        rate_match = re.search(
            r'전월신청률\s*:\s*([\d.]+)%.*?전월승인율\s*:\s*([\d.]+)%?',
            block, re.DOTALL
        )
        if rate_match:
            request_rate = float(rate_match.group(1))
            approval_rate = float(rate_match.group(2))
        else:
            request_rate = 0.0
            approval_rate = 0.0

        # 프로모션 여부
        has_promotion = 'thumb_sum_event_red' in block

        return Product(
            id=pid,
            name=name,
            merchant=name,
            category=category,
            category_raw=category_raw,
            approval_price=approval_price,
            request_rate=request_rate,
            approval_rate=approval_rate,
            has_promotion=has_promotion,
            detail_url=detail_url,
            source="replyalba",
        )


# ============================================================
# Mock 데이터
# ============================================================
def get_mock_products():
    return [
        Product("m01", "인공관절직업병보상센터", "인공관절직업병보상센터", Category.INSURANCE, "보험/상조", 200000, 2.5, 85.0, True, "#"),
        Product("m02", "미즈케어 솔루션", "미즈케어", Category.HEALTH, "건강/다이어트", 140000, 1.2, 78.0, True, "#"),
        Product("m03", "법무법인 서앤율", "법무법인 서앤율", Category.RECOVERY, "개인회생/법률", 60000, 3.5, 93.0, True, "#"),
        Product("m04", "광동 침향환 프리미엄", "광동제약", Category.HEALTH, "건강/다이어트", 61000, 1.5, 84.0, True, "#"),
        Product("m05", "서울 웨딩드레스 페어", "웨딩", Category.WEDDING, "결혼", 34000, 1.87, 85.0, True, "#"),
        Product("m06", "카슐랭", "카슐랭", Category.RENTAL, "렌트/리스", 45000, 0.15, 85.8, True, "#"),
        Product("m07", "로또추천번호", "로또", Category.LOTTO, "로또", 5000, 4.6, 84.7, True, "#"),
        Product("m08", "모두클린", "모두클린", Category.LIFE, "라이프", 8000, 5.13, 100.0, True, "#"),
    ]


class MockScraper:
    def fetch_products(self):
        return get_mock_products()


# ============================================================
# 메인 파이프라인
# ============================================================
def run_pipeline(scraper, analyzer, repository, notifier):
    print(f"\n⏰ [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 파이프라인 시작")
    print("🔍 상품 수집 중...")
    products = scraper.fetch_products()
    print(f"   → {len(products)}개 발견")

    known = repository.get_known_ids()
    new = [p for p in products if p.id not in known]
    print(f"   → 신규: {len(new)}개 (기존: {len(known)}개)")

    if not new:
        print("📭 신규 상품 없음.")
        return

    print("📊 분석 중...")
    recommended = analyzer.analyze(new)
    print(f"   → 추천: {len(recommended)}개 (기준: {analyzer.min_score}점 이상, {analyzer.min_price:,}원 이상)")

    for item in recommended[:5]:
        print(f"      - {item.product.name} ({item.product.approval_price:,}원, {item.score}점)")

    repository.save_products(new)

    if recommended:
        print("📱 알림 발송 중...")
        notifier.notify(recommended)
        print("✅ 완료!")
    else:
        print("📭 기준 이상인 상품이 없습니다.")


def main():
    args = sys.argv[1:]

    if "--test-discord" in args:
        print("🔔 디스코드 테스트 중...")
        ok = DiscordNotifier(DISCORD_WEBHOOK_URL).send_test()
        print("✅ 성공!" if ok else "❌ 실패!")
        return

    scraper = ReplyAlbaScraper(max_pages=20) if "--live" in args else MockScraper()
    print("🌐 머천트 리스트 크롤링 (전체)" if "--live" in args else "🧪 테스트 모드")

    analyzer = ProductAnalyzer(MIN_SCORE, MIN_APPROVAL_PRICE)
    repository = JsonRepository(DATA_FILE)

    if "--discord" in args:
        notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
        print("🔔 디스코드 알림")
    else:
        notifier = ConsoleNotifier()
        print("🖥️ 콘솔 출력")

    print(f"📋 설정: 점수 {MIN_SCORE}점↑, 단가 {MIN_APPROVAL_PRICE:,}원↑")
    print("\n🚀 시작!\n")
    run_pipeline(scraper, analyzer, repository, notifier)


if __name__ == "__main__":
    main()
