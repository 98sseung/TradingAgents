# Trading Bot — 0단계 (분석·측정)

TradingAgents 분석을 대기열로 돌리고, GUI에서 에이전트별 진행 상황과 ChatGPT 플랜 사용량을 봅니다.
매매는 하지 않습니다. 원본 `tradingagents/`, `cli/`는 수정하지 않고 가져다 씁니다.

## 설치 (최초 1회)

```bash
source .venv/bin/activate
pip install -r autotrader/requirements.txt
```

LLM 설정은 CLI와 같이 `.env`의 `TRADINGAGENTS_*` 값을 씁니다 (provider `chatgpt`, 모델, 언어, 토론 라운드).

## 실행 (터미널 두 개)

```bash
python -m autotrader.worker   # 분석 워커: 대기열의 분석을 하나씩 실행
python -m autotrader.gui      # 관리 화면: http://<이 PC의 IP>:8080 (예: http://192.168.189.131:8080)
```

- GUI는 기본으로 모든 네트워크에서 접속을 받습니다. 아직 로그인이 없으므로 같은 네트워크의 누구나 분석을 추가할 수 있습니다. 이 PC에서만 열려면 `TRADING_BOT_HOST=127.0.0.1`을 `.env`에 넣으세요.

- 워커와 GUI는 서로 독립입니다. GUI를 꺼도 분석은 계속되고, 워커가 꺼지면 GUI의 Master가 빨간색이 됩니다.
- 워커가 분석 도중 꺼지면, 다음에 켤 때 그 분석은 '실패'로 표시됩니다.
- 기록은 `~/.trading-bot/bot.db`(바꾸려면 `TRADING_BOT_DB`), 리포트는 `~/.tradingagents/logs/reports/`에 저장됩니다.

## 0단계에서 할 일

코인 5개 정도를 하루 1번씩 1주일 돌리고, 화면의 "오늘" 줄(분석 수, LLM 호출, 토큰, 평균 시간, 실패)을 기록합니다.
플랜 한도에 걸리면 해당 분석이 '실패'로 남고 오류 내용이 보드에 표시됩니다.

## 테스트

```bash
python -m pytest autotrader -q -o addopts=""
```
