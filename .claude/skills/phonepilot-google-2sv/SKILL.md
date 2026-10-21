---
name: phonepilot-google-2sv
description: Create one Google account with the user present and turn on 2-Step Verification using an authenticator (OTP) app only, then save the 10 backup codes to the recovery vault. Never registers a phone number as a second step.
---

# 구글 계정 + OTP 앱 2단계 인증 (워크플로 A)

1. `list_secret_refs(scope="GOOGLE")`와 `list_secret_refs(scope="PROFILE")`로 필요한 키가 있는지 확인한다(GOOGLE_<별칭>_PASSWORD, _DESIRED_USERNAMES, PROFILE_<별칭>_FIRST_NAME/_LAST_NAME/_BIRTH_DATE/_GENDER). 없으면 사용자에게 `.env`에 추가하라고 알린다(값을 채팅으로 받지 않는다).
2. `run_workflow(name="google.account_with_totp", inputs={"alias": "MAIN"})` 후 `workflow_status`를 5~10초 간격으로 확인한다.
3. `WAITING_HUMAN`이면 "PhonePilot 창의 안내를 따라 주세요"라고 사용자에게 알린다(OTP 등록·코드 입력·로봇/전화 확인은 사람이 한다).
4. 백업 코드는 `capture_backup_codes`가 보관함에 저장한다. 코드를 대화에 옮기지 않는다.
5. 완료되면 `report.two_step.phone_methods == 0`인지 확인한다. 1 이상이면 상태는 NEEDS_ATTENTION이며, 사용자에게 알리고 승인을 받아 제거한다.
6. 계정 생성은 24시간에 1회로 제한된다.
