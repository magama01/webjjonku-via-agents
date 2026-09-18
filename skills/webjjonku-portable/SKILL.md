---
name: webjjonku-portable
description: 셸을 호출할 수 있는 에이전트에서 웹쫀쿠 Oracle 실행기를 사용하는 공통 규칙
---

# 웹쫀쿠 공통 실행

이 스킬은 Codex, Claude Code, agy 등 로컬 셸 명령을 실행할 수 있는 에이전트에서
동일한 Oracle 웹 ChatGPT 실행기를 사용하기 위한 공통 계약임.

## 실행기

저장소 기본 경로:

`/Users/officener/dev/jjonku/codex-web-gpt-automation`

실행기는 `bin/chatgpt_oracle_run.py`임. `CODEX_HOME`에 설치된 실행기가 있으면
그 복사본을 사용해도 되지만, 설치 여부가 불확실하면 저장소 경로를 직접 사용함.

## 새 작업

1. 승인된 프로젝트 루트 안에 미션 파일을 작성함.
2. 아래 명령으로 먼저 dry-run 검증함. 세션 내에서 Project를 재사용하려면 `--session-id`를 지정하거나 에이전트 세션 환경변수를 활용함.
3. 사용자가 웹 실행을 요청했거나 에이전트가 웹 위임 권한을 받은 경우에만
   `--dry-run`을 제거하고 실행함.

```bash
python3 /Users/officener/dev/jjonku/codex-web-gpt-automation/bin/chatgpt_oracle_run.py \
  execute \
  --project-root "/absolute/project/root" \
  --mission-path "/absolute/project/root/.codex-tmp/web-mission.md" \
  --session-id "my-session-id" \
  --model gpt-5.6-sol \
  --effort extended \
  --dry-run
```

실제 실행은 위 명령에서 `--dry-run`만 제거함. 실행마다 새로운 `run_id`가 발급되지만,
`--session-id`가 지정되었거나 에이전트 세션 환경변수가 감지되면 **세션당 1개의 workspace Project를 생성하고 이후 실행에서 재사용**함. 세션 식별자가 없으면 단발성 실행으로 격리됨.

### 세션 식별자 감지 순서
1. CLI `--session-id`
2. `WEBJJONKU_SESSION_ID`
3. `CODEX_SESSION_ID`
4. `AGY_CONVERSATION_ID` / `ANTIGRAVITY_CONVERSATION_ID`
5. `CLAUDE_CONVERSATION_ID` / `CLAUDE_SESSION_ID`

## 재연결

응답 대기 중 연결이 끊기면 새 실행을 시작하지 않고 기존 run 디렉터리를 지정함.

```bash
python3 /Users/officener/dev/jjonku/codex-web-gpt-automation/bin/chatgpt_oracle_run.py \
  reconnect \
  --run-dir "/absolute/run/directory" \
  --dry-run
```

재연결 시 같은 `run_id`의 Project URL을 재사용함. 프롬프트를 자동 재전송하거나
다른 에이전트의 탭을 인수하지 않음.

## 에이전트별 규칙

- 이 문서는 에이전트 공통 규칙임. Claude/agy는 자신의 스킬 또는 시스템 지시에서
  이 파일을 참조하면 됨.
- 일반 대화의 모든 작업을 자동으로 웹에 넘기는 규칙은 아님. 사용자가 웹 위임을
  요청했거나 해당 에이전트의 별도 라우팅 규칙이 있을 때만 실행함.
- 인증, 승인된 root, DevSpace, 브라우저 프로필은 변경하지 않음.
- 소스 수정 작업은 미션에 쓰기 범위와 검증 명령을 명시함.
