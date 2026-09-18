# 외부 에이전트 연동

웹쫀쿠 실행기는 Codex 전용 API가 아니라 로컬 Python CLI임. 따라서 Claude Code,
agy 등 셸 명령을 실행할 수 있는 에이전트에서 같은 실행기를 호출할 수 있음.

## 설치 (Claude Code · Codex · agy 공통)

실행기는 저장소 `install.py`가 `${CODEX_HOME:-$HOME/.codex}`에 배포함. 스킬은
[skills CLI](https://skills.sh)로 세 에이전트에 한 번에 추가함.

```bash
git clone https://github.com/magama01/webjjonku-via-agents.git && cd webjjonku-via-agents && python3 install.py
npx skills add magama01/webjjonku-via-agents -s wjk -g -a claude-code -a codex -a antigravity
```

결과: `~/.agents/skills/wjk` (Codex·Antigravity가 직접 읽음), `~/.claude/skills/wjk` (심링크).
갱신은 `npx skills update wjk`.

## Claude Code

`/wjk <요청>` 한 번으로 미션 작성·실행·회수까지 맡김.

세션당 Project 재사용에는 세션 ID가 필요한데 Claude Code는 Bash 환경에 세션 ID를
넘기지 않음. 아래 스크립트가 `~/.claude/settings.json`의 `hooks.SessionStart`에
`WEBJJONKU_SESSION_ID=claude-<session_id>`를 `CLAUDE_ENV_FILE`로 내보내는 훅을 추가함
(멱등, 새 세션부터 적용, 훅 실행에 `jq` 필요).

```bash
sh ~/.agents/skills/wjk/setup-claude-hook.sh
```

훅 없이도 `/wjk`는 동작하되 매 실행이 단발(새 Project)임.

## Codex · agy

같은 `wjk` 스킬을 `$wjk` 또는 이름으로 호출함. 세션 ID는 `CODEX_SESSION_ID`,
`AGY_CONVERSATION_ID` / `ANTIGRAVITY_CONVERSATION_ID`가 환경에 있으면 자동 감지됨.
없으면 `--session-id`를 직접 넘기거나 단발 실행됨.

셸을 못 쓰는 에이전트는 이 방식으로 사용할 수 없음.

## 동작 경계

- 새 실행: 새 `run_id` 발급. Project는 세션(`--session-id` 또는 세션 환경변수)당 최초 1회만 생성하고 같은 세션의 이후 실행은 그 Project를 재사용함. 세션 식별자가 없으면 단발 실행으로 격리됨
- 같은 실행 재연결: 기존 `run_id`와 Project URL 재사용
- Project 이름: workspace basename, 동명 허용
- 일반 Codex/Claude 대화를 자동으로 웹으로 라우팅하지는 않음
- 인증·OAuth·승인 root·브라우저 프로필은 수동 상태로 유지
