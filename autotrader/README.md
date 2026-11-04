# autotrader — Binance 선물 자동매매 (1단계: 데모)

실행 방법과 사용법은 저장소 루트의 [`PLAY_README.md`](../PLAY_README.md)에 있습니다. 이 문서는 코드 구조만 다룹니다.
원본 `tradingagents/`, `cli/`는 수정하지 않고 가져다 씁니다.
`feedback.py`는 매시간 완료된 모든 분석을 실전 공개 시세로 1일·5일 후 채점해 `outcomes`에 저장하고 GUI에 적중률과 결과를 표시합니다.
분석 에이전트에는 Binance USDT 무기한 선물(격리, 최대 3배), 롱·숏 가능, 최대 5일 보유 및 Sell=숏 진입이라는 매매 지침을 전달하며 현재 보유 내역은 전달하지 않습니다.
급변 감지는 일일 적격 종목 전체를 5분마다 확인해 4시간 변동이 전일 ATR14의 1.5배를 돌파하면 UTC 하루 1건을 등록합니다(일일 분석 예산 공유). LLM 동의 시 고정 1.5 ATR 손절·3 ATR 익절·5일 만료로 진입합니다.
8주 데모 비교용으로 거부된 급변 분석도 `outcomes.mech_atr`에 120시간 기계적 추종 성과(왕복 수수료 0.10% 차감)를 저장합니다.

## 프로세스 (서로 독립, SQLite 하나만 공유 · `python -m autotrader`로 한 번에 실행)

| 모듈 | 실행 | 하는 일 |
| --- | --- | --- |
| `guard.py` | `python -m autotrader.guard` | Risk Guard. 5초마다 한도·KILL, 30초마다 원장 대사, 5분마다 보유기간 초과 청산 |
| `worker.py` | `python -m autotrader.worker` | 대기열 분석 실행(TradingAgents). `TRADING_BOT_WORKERS`개가 동시에 돌며 같은 종목은 겹치지 않음. 체크포인트로 크래시 후 이어서 실행, 죽은 워커의 작업만 회수 |
| `engine.py` | `python -m autotrader.engine` | Master. 매일 00:05 UTC 종목 선정·분석 등록, 이벤트 재분석, 판정·주문 실행 |
| `gui.py` | `python -m autotrader.gui` | 관리 화면(로그인 필요). 명령은 `control` 테이블에 쓰기만 함 |

## 라이브러리

| 모듈 | 내용 |
| --- | --- |
| `binance.py` | Binance USDⓈ-M 클라이언트(데모/실전). 키는 `~/.config/trading-bot/secrets.env`. 거래량·심볼 분류는 실전 공개 데이터 사용 |
| `trading.py` | 리스크 게이트(`decide`), 주문 실행(`execute`), 원장 대사·청산·한도. 규칙 상수는 파일 맨 위 |
| `scanner.py` | 후보 선정: 거래대금 상위 20 코인 → 밈·90일 미만 제외 → 추세 뚜렷한 5개 |
| `board.py` | 에이전트 보드(분석 단계별 상태) |
| `db.py` | 스키마: `jobs`, `decisions`, `trades`, `control`, `events`, `heartbeat` |

## 안전 원칙

- LLM 의견은 주문을 직접 내지 않습니다. `trading.decide`의 결정론적 게이트를 통과해야 하고, 모든 판정은 이유와 함께 `decisions`에 남습니다.
- 주문 client id는 판정 id에서 만들어져, 재실행해도 중복 주문이 나가지 않습니다.
- 진입 후 손절 주문을 걸지 못하면 즉시 청산합니다. 청산도 실패하면 KILL로 잠급니다.
- `source='manual'`(GUI) 분석은 절대 매매로 이어지지 않습니다.

## 테스트

```bash
python -m pytest autotrader -q -o addopts=""
```
