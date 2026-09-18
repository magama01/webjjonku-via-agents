# 외부 에이전트 연동

웹쫀쿠 실행기는 Codex 전용 API가 아니라 로컬 Python CLI임. 따라서 Claude Code,
agy 등 셸 명령을 실행할 수 있는 에이전트에서 같은 실행기를 호출할 수 있음.

## Claude Code

Claude가 스킬을 자동 검색하는 위치에 공통 스킬을 복사함.

```bash
mkdir -p "$HOME/.claude/skills/webjjonku-portable"
cp /Users/officener/dev/jjonku/codex-web-gpt-automation/skills/webjjonku-portable/SKILL.md \
  "$HOME/.claude/skills/webjjonku-portable/SKILL.md"
```

Claude에게는 `웹쫀쿠로 분석해줘`처럼 요청하면 됨. 스킬은 미션 파일 작성 후
`bin/chatgpt_oracle_run.py execute`를 호출하도록 안내함.

## agy 및 기타 에이전트

고정된 스킬 디렉터리 규약이 없는 에이전트는 다음 파일을 프로젝트 지시 또는
에이전트 시스템 프롬프트에 포함함.

`/Users/officener/dev/jjonku/codex-web-gpt-automation/skills/webjjonku-portable/SKILL.md`

그 후 에이전트가 동일한 `python3 .../bin/chatgpt_oracle_run.py execute` 명령을
호출하면 됨. 에이전트가 셸 실행을 지원하지 않으면 이 방식으로는 사용할 수 없음.

## 동작 경계

- 새 실행: 새 `run_id` 발급. Project는 세션(`--session-id` 또는 세션 환경변수)당 최초 1회만 생성하고 같은 세션의 이후 실행은 그 Project를 재사용함. 세션 식별자가 없으면 단발 실행으로 격리됨
- 같은 실행 재연결: 기존 `run_id`와 Project URL 재사용
- Project 이름: workspace basename, 동명 허용
- 일반 Codex/Claude 대화를 자동으로 웹으로 라우팅하지는 않음
- 인증·OAuth·승인 root·브라우저 프로필은 수동 상태로 유지
