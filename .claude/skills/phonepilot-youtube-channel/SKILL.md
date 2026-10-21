---
name: phonepilot-youtube-channel
description: Plan a YouTube channel with the user (name, handle, category, description, branding, thumbnail style per PRD 15) and create it in the YouTube app via the youtube.create_channel workflow.
---

# 유튜브 채널 기획 + 생성 (워크플로 B)

1. 기획 인터뷰(PRD 15.2의 10문항)를 사용자와 진행하고, 이름 후보 5개·핸들 후보 5개(3~30자 영문/숫자/._-)·설명 초안 2개(첫 2줄에 대상·주제·주기)·키워드 10~15개·썸네일 스타일 2안(15.7 A~F)을 제안한다.
2. 사용자가 고르면 `run_workflow(name="youtube.create_channel", inputs={channel_name, handle, handle_alt, description, profile_image, banner_image})`.
3. 이미지는 허용 폴더(설정 allowed_push_dirs)에 있어야 한다. 규격: 프로필 800×800, 배너 2560×1440(안전 영역 1235×338), 썸네일 1280×720.
4. 채널 생성 직전 승인 창이 뜬다. 끝나면 채널 페이지를 `observe`해 이름·핸들을 확인하고 보고한다.
