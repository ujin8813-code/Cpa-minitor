"""
CPA 상품 모니터링 시스템 (올인원 버전) v3.0

리플알바에서 돈 되는 상품을 자동으로 찾아 디스코드로 알림을 보냅니다.

사용법:
    python main.py                  → Mock 데이터 테스트 (화면 출력)
    python main.py --live           → 실제 크롤링 (화면 출력)
    python main.py --live --discord → 실제 크롤링 + 디스코드 알림
    python main.py --test-discord   → 디스코드 연결 테스트
    python main.py --schedule       → 6시간마다 자동 실행 (크롤링 + 디스코드)
"""

import json
import os
import re
import sys
import time
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
SCHEDULE_INTERVAL = 6 * 60 * 60  # 6시간 (초)


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
    OTHER = "기타"


CATEGORY_KEYWORDS = {
    Category.WEDDING: ["결혼", "웨딩", "박람회", "예식", "혼수", "신혼"],
    Category.DIET: ["다이어트", "체중", "살빼", "비감", "슬림"],
    Category.INSURANCE: ["보험", "보상", "직업병", "산재", "상조"],
    Category.RECOVERY: ["회생", "파산", "빚", "채무", "탕감"],
    Category.HEALTH: ["건강", "침향", "홍삼", "비타", "유산균", "영양", "솔루션", "케어"],
    Category.FINANCE: ["대출", "재테크", "투자", "주식", "금융"],
    Category.LEGAL: ["법률", "법무", "변호사", "법인", "검사", "사무소"],
}

CATEGORY_WEIGHTS = {
    Category.WEDDING: 1.0, Category.RECOVERY: 0.9,
    Category.INSURANCE: 0.85, Category.LEGAL: 0.85,
    Category.FINANCE: 0.8, Category.HEALTH: 0.7,
    Category.DIET: 0.6, Category.OTHER: 0.5,
}


def guess_category(name):
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return cat
    return Category.OTHER


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    merchant: str
    category: Category
    base_price: int
    promo_price: int
    approval_price: int
    start_date: datetime
    end_date: datetime
    source: str = "replyalba"

    @property
    def is_active(self):
        return self.start_date <= datetime.now() <= self.end_date

    @property
    def days_remaining(self):
        return max(0, (self.end_date - datetime.now()).days)

    @property
    def promo_ratio(self):
        return self.promo_price / self.base_price if self.base_price > 0 else 0.0


# ============================================================
# 분석기 — 좋은 상품 골라내기
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
        scored = [self._score(p) for p in products if p.is_active]
        recommended = [s for s in scored if s.score >= self.min_score]
        return sorted(recommended, key=lambda s: s.score, reverse=True)

    def _score(self, p):
        reasons = []
        score = 0.0

        # 승인단가 (40점)
        score += min(p.approval_price / 200000, 1.0) * 40
        if p.approval_price >= self.min_price:
            reasons.append(f"💰 고단가 {p.approval_price:,}원")

        # 프로모션 (30점)
        if p.base_price > 0:
            score += min(p.promo_price / p.base_price, 2.0) / 2.0 * 30
            if p.promo_price > p.base_price:
                reasons.append(f"🔥 프로모션 +{p.promo_price - p.base_price:,}원")

        # 잔여기간 (20점)
        score += min(p.days_remaining / 30, 1.0) * 20
        if p.days_remaining >= 14:
            reasons.append(f"📅 {p.days_remaining}일 남음")

        # 카테고리 (10점)
        cat_score = CATEGORY_WEIGHTS.get(p.category, 0.5) * 10
        score += cat_score
        if cat_score >= 8:
            reasons.append(f"📌 {p.category.value} (인기)")

        return ScoredProduct(product=p, score=round(score, 1), reasons=reasons)


# ============================================================
# 저장소 — JSON 파일로 상품 저장
# ============================================================
class JsonRepository:
    def __init__(self, path=DATA_FILE):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def save_products(self, products):
        existing = self._load()
        for p in products:
            existing[p.id] = {
                "id": p.id, "name": p.name, "merchant": p.merchant,
                "category": p.category.value, "base_price": p.base_price,
                "promo_price": p.promo_price, "approval_price": p.approval_price,
                "start_date": p.start_date.isoformat(),
                "end_date": p.end_date.isoformat(), "source": p.source,
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
            icon = "🔥" if item.score >= 80 else "✅"
            lines.append(f"{icon} **[{i}] {p.name}**")
            lines.append(f"   승인단가: **{p.approval_price:,}원** | 점수: {item.score}점")
            lines.append(f"   기본: {p.base_price:,}원 / 프로모션: {p.promo_price:,}원")
            lines.append(f"   카테고리: {p.category.value} | {p.days_remaining}일 남음")
            if item.reasons:
                lines.append(f"   {' | '.join(item.reasons)}")
            lines.append("")

        message = "\n".join(lines)
        if len(message) > 1900:
            message = message[:1900] + "\n... (더 많은 상품이 있습니다)"

        self._send({"content": message})

    def send_test(self):
        try:
            self._send({"content": "🔔 CPA 모니터 연결 성공! 이제 추천 상품 알림을 받습니다."})
            return True
        except Exception as e:
            print(f"디스코드 에러: {e}")
            return False

    def _send(self, payload):
        data = json.dumps(payload)
        print(f"   디스코드 전송 중... (메시지 {len(data)}자)")
        result = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", data,
                self._url,
            ],
            capture_output=True, text=True, timeout=15,
        )
        code = result.stdout.strip()
        print(f"   디스코드 응답: {code}")
        if code not in ("200", "204"):
            result2 = subprocess.run(
                [
                    "curl", "-s",
                    "-X", "POST",
                    "-H", "Content-Type: application/json",
                    "-d", data,
                    self._url,
                ],
                capture_output=True, text=True, timeout=15,
            )
            print(f"   디스코드 에러 상세: {result2.stdout}")
            raise Exception(f"Discord error: {code}")


# ============================================================
# 콘솔 알림 (테스트용)
# ============================================================
class ConsoleNotifier:
    def notify(self, scored_products):
        if not scored_products:
            print("\n📭 추천할 신규 상품이 없습니다.\n")
            return
        print(f"\n{'='*55}")
        print(f"🚨 추천 상품 {len(scored_products)}개 발견!")
        print(f"{'='*55}")
        for i, item in enumerate(scored_products, 1):
            p = item.product
            icon = "🔥" if item.score >= 80 else "✅"
            print(f"\n{icon} [{i}] {p.name}")
            print(f"   점수: {item.score}점 | 승인단가: {p.approval_price:,}원")
            print(f"   기본: {p.base_price:,}원 → 프로모션: {p.promo_price:,}원")
            print(f"   카테고리: {p.category.value} | {p.days_remaining}일 남음")
            if item.reasons:
                print(f"   이유: {' | '.join(item.reasons)}")
        print(f"\n{'='*55}\n")


# ============================================================
# 리플알바 크롤러 (v3 — HTML 구조 기반 정확한 파싱)
# ============================================================
def parse_date(s):
    try:
        return datetime.strptime(s, "%y.%m.%d")
    except ValueError:
        return datetime.now()


class ReplyAlbaScraper:
    """
    리플알바 HTML 구조 (디버그로 확인됨):
    
    ... 상품명 ... |기본단가| XX,XXX원| |프로모션| XX,XXX원| |승인시| XXX,XXX원| |YY.MM.DD ~ YY.MM.DD| ...
    
    즉, 순서가: 상품명 → 기본단가 → 프로모션 → 승인단가 → 날짜
    상품명은 "기본단가" 키워드 바로 앞에 있음!
    """
    URL = "https://www.replyalba.com/mobile/promotion/pro.php"

    def __init__(self, max_pages=5):
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
                    break
                for p in products:
                    if p.id not in seen:
                        all_products.append(p)
                        seen.add(p.id)
                print(f"   → {len(products)}개 수집")
            except Exception as e:
                print(f"   ❌ {page}페이지 실패: {e}")
                break
        print(f"   → 총 {len(all_products)}개 (중복 제거)")
        return all_products

    def _fetch(self, page):
        url = f"{self.URL}?page={page}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "text/html", "Accept-Language": "ko-KR,ko",
        })
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.read().decode("utf-8", errors="replace")

    def _parse(self, html):
        products = []
        seen_ids = set()

        # ★ 핵심 변경: "기본단가" 패턴을 기준으로 상품 블록을 찾음
        # 디버그에서 확인된 구조:
        # ...상품명...|기본단가| 150,000원| |프로모션| 50,000원| |승인시| 200,000원| |26.02.01 ~ 26.02.28|
        
        # 상품 블록 패턴: 기본단가 ~ 승인시 ~ 날짜까지 한 덩어리
        block_pat = re.compile(
            r'기본단가[^0-9]*?(\d{1,3}(?:,\d{3})*)\s*원'   # 기본단가
            r'.*?'
            r'프로모션[^0-9]*?(\d{1,3}(?:,\d{3})*)\s*원'    # 프로모션
            r'.*?'
            r'승인시?\s*[^0-9]*?(\d{1,3}(?:,\d{3})*)\s*원'  # 승인단가
            r'.*?'
            r'(\d{2}\.\d{2}\.\d{2})\s*~\s*(\d{2}\.\d{2}\.\d{2})',  # 날짜
            re.DOTALL
        )

        for i, m in enumerate(block_pat.finditer(html)):
            bp = int(m.group(1).replace(",", ""))
            pp = int(m.group(2).replace(",", ""))
            ap = int(m.group(3).replace(",", ""))
            sd = parse_date(m.group(4))
            ed = parse_date(m.group(5))

            # ★ 상품명: "기본단가" 앞쪽 텍스트에서 추출
            block_start = m.start()
            before_text = html[max(0, block_start - 500):block_start]
            
            # HTML 태그 제거하고 텍스트만
            clean_before = re.sub(r'<[^>]+>', ' ', before_text)
            clean_before = re.sub(r'\s+', ' ', clean_before).strip()
            
            # 상품명 추출: 마지막 의미있는 한글 텍스트
            name = self._extract_name(clean_before)
            
            # 상품 ID: 승인단가 + 기본단가 조합 (고유하게)
            pid = f"ra-{ap}-{bp}-{pp}"
            
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            products.append(Product(
                id=pid, name=name, merchant=name,
                category=guess_category(name),
                base_price=bp, promo_price=pp, approval_price=ap,
                start_date=sd, end_date=ed, source="replyalba",
            ))
        
        return products

    def _extract_name(self, text):
        """기본단가 앞쪽 텍스트에서 상품명 추출"""
        
        # 쓸모없는 단어들
        SKIP = [
            "기본단가", "프로모션", "승인", "전월", "당월", "신청률",
            "승인율", "순위", "정렬", "검색", "조회", "로그인",
            "공지사항", "이용약관", "개인정보", "카테고리", "필터",
            "전체보기", "더보기", "리플알바", "페이지", "머천트",
            "안내해드리겠습니다", "변경될", "요청에", "광고주",
            "공지사항으로", "변경", "의해", "리스트",
        ]
        
        # 뒤에서부터 의미있는 한글 단어 찾기
        # "... | | 인공관절직업병보상센터 | |기본단가|" 에서 "인공관절직업병보상센터" 찾기
        parts = re.split(r'[|\s]+', text)
        parts = [p.strip() for p in parts if p.strip()]
        
        # 뒤에서부터 탐색 (기본단가에 가장 가까운 게 상품명)
        for part in reversed(parts):
            # 한글이 2자 이상 포함되어야 함
            korean_chars = re.findall(r'[가-힣]', part)
            if len(korean_chars) < 2:
                continue
            # skip 단어 포함하면 넘기기
            if any(s in part for s in SKIP):
                continue
            # 숫자로만 되어있으면 넘기기
            if re.match(r'^[\d,.\s]+$', part):
                continue
            # 너무 짧거나 긴 건 넘기기
            if len(part) < 2 or len(part) > 50:
                continue
            return part
        
        return f"상품 #{hash(text) % 1000}"


# ============================================================
# Mock 데이터 (테스트용)
# ============================================================
def get_mock_products():
    now = datetime.now()
    return [
        Product("m01", "인공관절직업병보상센터", "인공관절직업병보상센터", Category.INSURANCE,
                150000, 50000, 200000, datetime(2026,2,1), datetime(2026,2,28)),
        Product("m02", "미즈케어 솔루션", "미즈케어", Category.HEALTH,
                90000, 50000, 140000, datetime(2026,2,1), datetime(2026,2,28)),
        Product("m03", "직업병보상전문상담센터", "직업병보상전문상담센터", Category.INSURANCE,
                40000, 50000, 90000, datetime(2026,2,1), datetime(2026,2,28)),
        Product("m04", "부장검사 출신 법률사무소 청하", "청하", Category.RECOVERY,
                40000, 32000, 72000, datetime(2026,2,1), datetime(2026,2,28)),
        Product("m05", "개인회생법률상담센터", "동윤", Category.RECOVERY,
                40000, 30000, 70000, now-timedelta(days=2), now+timedelta(days=45)),
        Product("m06", "광동 침향환 프리미엄", "광동제약", Category.HEALTH,
                32000, 47000, 79000, datetime(2026,2,1), datetime(2026,2,28)),
        Product("m07", "개인회생 새출발의 전환점", "법률사무소", Category.RECOVERY,
                40000, 38000, 78000, now-timedelta(days=5), now+timedelta(days=40)),
        Product("m08", "인공관절보험보상센터", "보험보상센터", Category.INSURANCE,
                150000, 50000, 200000, now-timedelta(days=10), now+timedelta(days=35)),
        Product("m09", "테스트 저단가", "테스트", Category.OTHER,
                5000, 5000, 10000, now-timedelta(days=1), now+timedelta(days=10)),
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
    print(f"   → 추천: {len(recommended)}개 (기준: {analyzer.min_score}점 이상)")

    # 추천 상품 미리보기
    for item in recommended[:5]:
        print(f"      - {item.product.name} ({item.product.approval_price:,}원, {item.score}점)")

    repository.save_products(new)

    if recommended:
        print("📱 알림 발송 중...")
        notifier.notify(recommended)
        print("✅ 완료!")
    else:
        print("📭 기준 점수 이상인 상품이 없습니다.")


# ============================================================
# 스케줄러 — 6시간마다 자동 실행
# ============================================================
def run_scheduler(interval=SCHEDULE_INTERVAL):
    print("=" * 55)
    print("🤖 CPA 자동 모니터링 시작!")
    print(f"   실행 간격: {interval // 3600}시간")
    print(f"   추천 기준: {MIN_SCORE}점 이상, {MIN_APPROVAL_PRICE:,}원 이상")
    print("=" * 55)

    scraper = ReplyAlbaScraper(max_pages=5)
    analyzer = ProductAnalyzer(MIN_SCORE, MIN_APPROVAL_PRICE)
    repository = JsonRepository(DATA_FILE)
    notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)

    run_count = 0
    while True:
        run_count += 1
        print(f"\n{'─' * 40}")
        print(f"📡 실행 #{run_count}")
        try:
            run_pipeline(scraper, analyzer, repository, notifier)
        except Exception as e:
            print(f"❌ 에러 발생: {e}")

        next_run = datetime.now() + timedelta(seconds=interval)
        print(f"\n⏳ 다음 실행: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n🛑 스케줄러 종료! (총 {run_count}회 실행)")
            break


def main():
    args = sys.argv[1:]

    if "--test-discord" in args:
        print("🔔 디스코드 테스트 중...")
        ok = DiscordNotifier(DISCORD_WEBHOOK_URL).send_test()
        print("✅ 성공!" if ok else "❌ 실패!")
        return

    if "--schedule" in args:
        run_scheduler()
        return

    scraper = ReplyAlbaScraper(max_pages=5) if "--live" in args else MockScraper()
    print("🌐 실제 크롤링" if "--live" in args else "🧪 테스트 모드")

    analyzer = ProductAnalyzer(MIN_SCORE, MIN_APPROVAL_PRICE)
    repository = JsonRepository(DATA_FILE)

    if "--discord" in args:
        notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
        print("🔔 디스코드 알림")
    else:
        notifier = ConsoleNotifier()
        print("🖥️ 콘솔 출력")

    print("\n🚀 시작!\n")
    run_pipeline(scraper, analyzer, repository, notifier)


if __name__ == "__main__":
    main()

