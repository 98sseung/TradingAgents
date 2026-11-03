# Trading Bot 실행 방법

`trading-bot` 브랜치의 0단계(분석·측정) 실행 순서입니다. 처음 한 번만 하는 단계는 **(최초 1회)**로 표시했습니다.

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
#FRED_API_KEY=            # 무료 키, 금리·물가 데이터 (선택)
#TRADING_BOT_HOST=127.0.0.1   # 이 PC에서만 GUI를 열 때
```

## 5. ChatGPT 로그인 (최초 1회)

```bash
tradingagents login chatgpt
```

출력된 주소를 브라우저에서 열어 로그인합니다. 로그인 정보는 `~/.tradingagents/chatgpt-auth.json`에 저장됩니다.

## 6. 실행 (터미널 두 개)

두 터미널 모두 먼저 저장소 폴더로 이동해 venv를 켭니다.

```bash
cd ~/Project/hub/TradingAgents
source .venv/bin/activate
```

**터미널 1 — 분석 워커** (대기열의 분석을 하나씩 실행)

```bash
python -m autotrader.worker
```

**터미널 2 — 관리 화면(GUI)**

```bash
python -m autotrader.gui
```

브라우저에서 접속합니다.

- 같은 PC: http://127.0.0.1:8080
- 다른 PC: http://192.168.189.131:8080 (이 VM의 IP. miniPC로 옮기면 그 IP로 접속)

다른 PC에서 접속이 안 되면 방화벽에서 포트를 엽니다.

```bash
sudo ufw allow 8080/tcp
```

종료는 각 터미널에서 `Ctrl+C`입니다.

## 7. 사용 방법

1. **새 분석**에 종목(쉼표로 여러 개, 예: `BTC-USD, ETH-USD`), 기준일, 분석가를 고르고 **대기열에 추가**를 누릅니다. 코인은 펀더멘털 분석가가 자동으로 빠집니다.
2. **Master** 패널: 워커가 켜져 있으면 초록색 '작업 중'과 지금 분석 중인 종목이 보입니다. 아래 줄은 오늘의 분석 수, LLM 호출, 토큰, 평균 시간입니다.
3. **에이전트 보드**: 에이전트별 상태(대기·작업 중·완료·오류)와 마지막 발언. 카드를 누르면 전체 내용, 완료되면 최종 의견과 HTML 리포트 링크가 나옵니다.
4. **분석 기록**: 행을 누르면 그 분석의 보드로 바뀝니다.

분석 하나는 수 분이 걸리고 ChatGPT 플랜 사용량을 씁니다. 0단계에서는 코인 5개 정도를 하루 1번씩 1주일 돌리고 Master 줄의 수치를 기록합니다.

## 저장 위치

| 내용 | 위치 |
| --- | --- |
| 분석 기록 DB | `~/.trading-bot/bot.db` (바꾸려면 `TRADING_BOT_DB`) |
| 리포트 | `~/.tradingagents/logs/reports/` |
| 결정 메모리 로그 | `~/.tradingagents/memory/trading_memory.md` |
| Binance 키 (1단계부터) | `~/.config/trading-bot/secrets.env` (권한 600, git에 넣지 않음) |

## 문제 해결

| 증상 | 원인과 해결 |
| --- | --- |
| Master가 빨간색 "워커가 꺼져 있습니다" | 터미널 1의 워커가 꺼져 있음. `python -m autotrader.worker` 실행 |
| 분석이 '실패', 오류에 로그인 안내 | `tradingagents login chatgpt` 다시 실행 |
| 분석이 '실패', 오류에 사용량·429 | 플랜 한도 도달. 잠시 뒤 다시 추가 |
| 분석이 '실패', 오류 "worker stopped mid-run" | 분석 도중 워커가 꺼졌음. 다시 추가 |
| `Address already in use` (8080) | GUI가 이미 켜져 있음. 기존 창을 쓰거나 끄고 다시 실행 |
| `ModuleNotFoundError` | venv가 꺼져 있음. `source .venv/bin/activate` 후 다시 실행 |

## 테스트

```bash
pip install pytest
python -m pytest autotrader -q -o addopts=""
```
