---
name: phonepilot-channel-launch
description: Policy-compliant channel launch for a new YouTube channel - profile completeness checklist, read-only competitor research via the YouTube Data API, first-4-week content plan. Use instead of any "aging" by automated watching, liking or subscribing, which this tool does not do.
---

# 채널 초기 운영 (워크플로 C)

- 자동 시청·스와이프·좋아요·구독으로 계정을 '에이징'하는 일은 하지 않는다(YouTube 허위 참여 정책 위반, 채널 해지 위험). 사용자가 요청하면 이유를 짧게 설명하고 아래 대안을 제안한다.
1. `run_workflow(name="youtube.channel_launch_checklist")`로 내 채널 화면을 읽기 전용으로 점검한다.
2. YouTube 연동이 켜져 있으면 `yt_search_channels(q=니치 키워드)` → 상위 5~10개 채널에 `yt_channel_report(channel_id)`로 업로드 주기, 요일·시간 분포, 쇼츠 비율, 아웃라이어 영상, 제목 패턴을 모은다(쿼터 1,000 이내).
3. 결과로 첫 4주 계획(PRD 16.4 표)과 영상별 제목 후보·설명·태그·썸네일 문구를 만든다. 업로드와 공개는 사용자가 한다.
