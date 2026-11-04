# Trading Bot 실행 방법

`trading-bot` 브랜치의 1단계(Binance 데모 자동매매) 실행 순서입니다. 처음 한 번만 하는 단계는 **(최초 1회)**로 표시했습니다.

## 1. 코드 받기 (최초 1회)

```bash
git clone https://github.com/98sseung/TradingAgents.git
cd TradingAgents
git checkout trading-bot
```

이미 받아 둔 저장소라면 `git checkout trading-bot && git pull`만 하면 됩니다.

## 2. 가상환경(venv) 만들기 (최초 1회)

Python 3.11 이상이 필요합니다(`python3 --version`으로 확인).

```bash
python3 -m venv .venv
```

Ubuntu에서 `ensurepip is not available` 오류가 나면 먼저 설치합니다.

```bash
sudo apt install python3-venv
```

## 3. 패키지 설치 (최초 1회, 코드 업데이트 후에도)

```bash
source .venv/bin/activate
pip install .
pip install -r autotrader/requirements.txt
```

## 4. 설정 파일 `.env` (최초 1회)

```bash
cp .env.example .env
```

`.env`에서 아래 값을 채웁니다(주석 `#` 제거).

```ini
TRADINGAGENTS_LLM_PROVIDER=chatgpt
TRADINGAGENTS_QUICK_THINK_LLM=gpt-6-luna
TRADINGAGENTS_DEEP_THINK_LLM=gpt-6-sol
TRADINGAGENTS_OUTPUT_LANGUAGE=Korean
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1
TRADINGAGENTS_MAX_RISK_ROUNDS=1
SEC_EDGAR_USER_AGENT=이름 이메일
TRADING_BOT_GUI_PASSWORD=8자이상비밀번호   # GUI 로그인 (없으면 GUI가 시작되지 않음)
#FRED_API_KEY=            # 무료 키, 금리·물가 데이터 (선택)
#TRADING_BOT_HOST=127.0.0.1   # 이 PC에서만 GUI를 열 때
#TRADING_BOT_MODE=demo        # demo(기본) | live (실전, 2단계 전까지 쓰지 말 것)
#TRADING_BOT_DAILY_BUDGET=12  # 엔진이 하루에 넣는 자동 분석 최대 개수
TRADING_BOT_WORKERS=5         # 동시에 분석할 개수 (1이면 하나씩). 5개로 10종목 약 5.4분
```

Binance 키는 `.env`가 아니라 별도 파일에 둡니다(권한 600).

```bash
mkdir -p ~/.config/trading-bot && chmod 700 ~/.config/trading-bot
install -m 600 /dev/null ~/.config/trading-bot/secrets.env   # 이미 있으면 생략
```

```ini
# ~/.config/trading-bot/secrets.env
BINANCE_TESTNET_API_KEY=Demo Trading에서 발급한 키
BINANCE_TESTNET_API_SECRET=Demo Trading에서 발급한 시크릿
```

연결 확인(조회만 하고 주문은 내지 않음): `python -m autotrader.binance`

## 5. ChatGPT 로그인 (최초 1회)

```bash
tradingagents login chatgpt
```

출력된 주소를 브라우저에서 열어 로그인합니다. 로그인 정보는 `~/.tradingagents/chatgpt-auth.json`에 저장됩니다.

## 6. 실행 (명령 하나)

```bash
cd ~/Project/hub/TradingAgents
source .venv/bin/activate
python -m autotrader
```

이 명령 하나가 아래 네 프로세스를 띄우고, 죽으면 다시 살리고, `Ctrl+C`로 모두 함께 끕니다. 각자 별도 프로세스라서 엔진이 멈춰도 Risk Guard는 계속 돕니다.

| 프로세스 | 역할 | 로그 |
| --- | --- | --- |
| guard | **Risk Guard**: 한도·KILL·손절 확인·원장 대사 | `~/.trading-bot/logs/guard.log` |
| worker-1 … worker-N | **Worker**: 대기열의 분석을 동시에 N개 실행 (`TRADING_BOT_WORKERS`). 같은 종목은 동시에 돌지 않음 | `~/.trading-bot/logs/worker-<n>.log` |
| engine | **Master**: 매일 09:05(KST) 종목 선정·분석 등록, 분석 결과로 판정·주문 | `~/.trading-bot/logs/engine.log` |
| gui | **GUI**: 관리 화면 | `~/.trading-bot/logs/gui.log` |

- 로그를 실시간으로 보려면 다른 터미널에서 `tail -f ~/.trading-bot/logs/*.log`
- 시작하자마자 꺼지는 프로세스(예: GUI 비밀번호 없음)는 다시 띄우지 않고 원인을 화면에 출력합니다.
- 하나만 따로 띄우고 싶으면 `python -m autotrader.guard`처럼 이름을 붙여 실행합니다(디버깅용).

브라우저에서 접속합니다.

- 같은 PC: http://127.0.0.1:8080
- 다른 PC: http://192.168.189.131:8080 (이 VM의 IP. miniPC로 옮기면 그 IP로 접속)

다른 PC에서 접속이 안 되면 방화벽에서 포트를 엽니다.

```bash
sudo ufw allow 8080/tcp
```

처음 접속하면 로그인 화면이 나옵니다. `.env`의 `TRADING_BOT_GUI_PASSWORD`를 입력하세요. 오른쪽 위 버튼으로 로그아웃합니다.

- Risk Guard가 꺼져 있으면 엔진은 **신규 진입을 하지 않습니다**(청산은 함). 거래소에 걸린 손절·익절 주문은 프로세스와 상관없이 Binance에서 동작합니다.
- 분석 도중 워커를 끄면, 다시 켰을 때 마지막으로 끝난 에이전트 다음부터 이어서 실행됩니다(두 번째로 끊기면 '실패' 처리).

## 7. 사용 방법

**자동매매 흐름** (사람이 할 일 없음)

1. Master가 매일 09:05(KST)에 고정 대상 BTC·ETH·XRP·SOL·LINK의 USDT 선물 중 거래 가능하고 완료 일봉 점수·Yahoo 동일 자산 검증을 통과한 종목을 추세 순으로, 보유 중인 코인과 함께 분석 대기열에 넣습니다(분석 기록에 `자동` 표시).
2. Worker가 분석하고, Master가 결과(최종 의견)를 리스크 게이트에 넣어 판정합니다. 판정과 이유는 보드의 **리스크 게이트** 줄과 **결정 · 이벤트** 패널에 남습니다.
3. 진입이면 Binance에 시장가 주문 + 손절·익절 주문을 겁니다. 포지션은 **포지션** 패널에 보입니다.
4. Risk Guard가 5초마다 한도를 확인하고, 30초마다 거래소와 원장을 맞추고, 5일 지난 포지션을 청산합니다.

고정 5종목은 TradingAgents가 암호화폐로 인식하므로 감성 분석에서 주식으로 오인하는 문제를 피합니다. LINK는 밈 코인 DOGE를 제외한 지원 종목 중 거래량이 가장 커서 포함했습니다. 거래대금 순위·상장 나이로 대상을 바꾸지 않으며, 급변 감지도 이 5종목 중 거래 가능하고 완료 일봉 점수를 얻은 종목을 사용합니다.

**버튼**

- **STOP**: 신규 진입만 멈춥니다. 기존 포지션의 손절·익절은 그대로입니다. 다시 누르면 재개.
- **KILL**: 모든 주문 취소 + 모든 포지션 시장가 청산 + 잠금. **잠금 해제** 전까지 어떤 진입도 하지 않습니다. 누적 손실 -30%에서는 자동으로 걸립니다.

**수동 분석**: 왼쪽 **새 분석**으로 넣은 분석은 읽기용입니다. **매매로 이어지지 않습니다.**

완료된 자동·수동 분석은 매시간 실전 공개 시세로 1일·5일 후 자동 채점되며, **분석 채점**과 **적중률 (5일)**(보류 제외, n=채점 수)에 표시됩니다.
`급변` 분석은 일일 적격 종목의 4시간 변동이 전일 ATR14의 1.5배를 돌파할 때 UTC 하루 최대 1건 등록되며 자동 분석과 예산을 공유합니다. LLM이 방향에 동의할 때만 고정 1.5 ATR 손절·3 ATR 익절·5일 만료로 진입합니다.
8주 데모에서는 LLM 거부 여부와 무관하게 5일 기계적 추종 성과를 DB `outcomes.mech_atr`에 저장해 비교합니다(왕복 수수료 0.10% 차감).

**규칙 요약** (`autotrader/trading.py` 맨 위 상수)

| 항목 | 값 |
| --- | --- |
| 포지션당 증거금 / 레버리지 | 잔고의 30% / 5배 (격리, 단방향). 손절이 진입가에서 12%보다 멀면 진입 안 함 |
| 동시 보유 | 최대 3개 |
| 진입 | Buy·Overweight → 롱, Sell → 숏 |
| 청산 | 롱은 Underweight·Sell, 숏은 Buy·Overweight, 또는 손절·익절, 5일 경과 |
| 손절 / 익절 | Trader 손절가(0.5~3 ATR 범위일 때) 아니면 1.5 ATR / PM 목표가 아니면 2R |
| 진입 차단 | KILL·STOP, 일일 -10%, 손절 후 24시간, 분석 6시간 경과, RSI 과열(롱>75·숏<25) |

## 저장 위치

| 내용 | 위치 |
| --- | --- |
| 분석·판정·거래·이벤트 DB | `~/.trading-bot/bot.db` (바꾸려면 `TRADING_BOT_DB`) |
| 리포트 | `~/.tradingagents/logs/reports/` |
| 결정 메모리 로그 | `~/.tradingagents/memory/trading_memory.md` |
| Binance 키 | `~/.config/trading-bot/secrets.env` (권한 600, git에 넣지 않음) |

## 문제 해결

| 증상 | 원인과 해결 |
| --- | --- |
| 상단 칩이 빨간색 (Master·Worker·Risk Guard 꺼짐) | `python -m autotrader` 실행 화면과 `~/.trading-bot/logs/<이름>.log` 확인 |
| 이벤트에 "entry waiting for a fresh Risk Guard heartbeat" | Risk Guard가 꺼져 있어 진입 보류. `python -m autotrader.guard` 실행 |
| `Secrets file permissions are too open` | `chmod 600 ~/.config/trading-bot/secrets.env` |
| 상단에 `KILL · 잠김` | KILL 상태. 원인(이벤트 패널)을 확인한 뒤 **잠금 해제** |
| 분석이 '실패', 오류에 로그인 안내 | `tradingagents login chatgpt` 다시 실행 |
| 분석이 '실패', 오류에 사용량·429 | 플랜 한도 도달. 잠시 뒤 다시 추가 |
| 분석이 '실패', 오류 "worker stopped mid-run twice" | 같은 분석 중 워커가 두 번 꺼졌음. 다시 추가 |
| GUI 실행 시 "Set TRADING_BOT_GUI_PASSWORD" | `.env`에 8자 이상 비밀번호 추가 |
| `Address already in use` (8080) | GUI가 이미 켜져 있음. 기존 창을 쓰거나 끄고 다시 실행 |
| `ModuleNotFoundError` | venv가 꺼져 있음. `source .venv/bin/activate` 후 다시 실행 |

## 테스트

```bash
pip install pytest
python -m pytest autotrader -q -o addopts=""
```
