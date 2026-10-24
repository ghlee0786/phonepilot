; PhonePilot installer (Inno Setup 6). Built by scripts/build_release.py:
;   ISCC /DAppVersion=x.y.z /DSourceDir=<dist\PhonePilot> installer\phonepilot.iss
; Per-user install (no admin): the in-app updater can run a new setup silently without a UAC prompt.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\PhonePilot"
#endif
#define AppName "PhonePilot"
#define AppExe "PhonePilot.exe"
#define CliExe "phonepilot-cli.exe"
#define ReleasesUrl "https://github.com/ghlee0786/phonepilot"
#define AppIdGuid "8F3C2B1E-5D4A-4C7B-9E21-6A0F3D5B7C91"

[Setup]
AppId={{{#AppIdGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=PhonePilot
AppPublisherURL={#ReleasesUrl}
AppSupportURL={#ReleasesUrl}/issues
AppUpdatesURL={#ReleasesUrl}/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist\installer
OutputBaseFilename=PhonePilot-{#AppVersion}-setup
SetupIconFile=..\src\phonepilot\gui\resources\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardSizePercent=130
WizardImageFile=assets\wizard.bmp
WizardSmallImageFile=assets\wizard_small.bmp
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=force
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "바로가기:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\feature_*.bmp"; Flags: dontcopy

[InstallDelete]
; files of the previous build that the new one no longer ships
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; also runs after a silent update (no skipifsilent), so the updated app comes back on its own
Filename: "{app}\{#AppExe}"; Description: "PhonePilot 실행"; Flags: nowait postinstall

[UninstallRun]
Filename: "{app}\_internal\vendor\platform-tools\adb.exe"; Parameters: "kill-server"; Flags: runhidden skipifdoesntexist; RunOnceId: "KillAdb"

[Code]
const
  FEATURE_COUNT = 4;

var
  FeaturePages: array[1..4] of TWizardPage;
  ClaudePage: TInputOptionWizardPage;
  KeyPage: TInputQueryWizardPage;
  PhonePage: TOutputMsgWizardPage;
  Upgrading: Boolean;

function FeatureTitle(I: Integer): String;
begin
  case I of
    1: Result := '폰 화면을 PC에서 그대로';
    2: Result := '반복 작업은 워크플로로';
    3: Result := '위험한 행동은 사람이 승인';
    4: Result := '연결·AI 설정을 한곳에서';
  end;
end;

function FeatureText(I: Integer): String;
begin
  case I of
    1: Result := 'USB나 Wi-Fi로 연결한 Android 폰을 저지연으로 미러링하고, 마우스와 키보드로 바로 조작합니다. 연결이 끊겨도 폰을 다시 꽂으면 자동으로 이어집니다. 오른쪽 타임라인에는 사람과 AI가 한 모든 동작이 남습니다.';
    2: Result := '자주 하는 일을 스킬(YAML)로 만들어 한 번에 실행합니다. 단계마다 결과를 확인하고, 막히면 사람에게 넘기거나 승인을 받습니다.';
    3: Result := '결제·게시·업로드·보안 설정처럼 되돌리기 어려운 행동은 AI가 요청해도 승인 창에서 사람이 허락해야 실행됩니다. [정지] 버튼이나 Pause 키로 언제든 AI를 멈출 수 있습니다.';
    4: Result := 'Claude Code·Claude Desktop이 MCP로 같은 화면을 보고 조작합니다. 비밀번호는 AI에게 보이지 않고 이름(키)만 전달됩니다. 업데이트 확인과 문제 신고도 여기서 합니다.';
  end;
end;

function IsUpgrade(): Boolean;
begin
  Result := RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdGuid}_is1');
end;

procedure AddFeaturePage(I: Integer; After: Integer);
var
  Page: TWizardPage;
  Img: TBitmapImage;
  Txt: TNewStaticText;
  ImgH, ImgW: Integer;
begin
  Page := CreateCustomPage(After, '주요 기능 ' + IntToStr(I) + '/' + IntToStr(FEATURE_COUNT), FeatureTitle(I));
  FeaturePages[I] := Page;
  ExtractTemporaryFile('feature_' + IntToStr(I) + '.bmp');

  Txt := TNewStaticText.Create(Page);
  Txt.Parent := Page.Surface;
  Txt.AutoSize := False;
  Txt.WordWrap := True;
  Txt.Left := 0;
  Txt.Width := Page.SurfaceWidth;
  Txt.Height := ScaleY(56);
  Txt.Top := Page.SurfaceHeight - Txt.Height;
  Txt.Caption := FeatureText(I);

  { 16:9 screenshot, as large as the space above the text allows }
  ImgH := Txt.Top - ScaleY(8);
  ImgW := ImgH * 16 div 9;
  if ImgW > Page.SurfaceWidth then
  begin
    ImgW := Page.SurfaceWidth;
    ImgH := ImgW * 9 div 16;
  end;
  Img := TBitmapImage.Create(Page);
  Img.Parent := Page.Surface;
  Img.Stretch := True;
  Img.Width := ImgW;
  Img.Height := ImgH;
  Img.Left := (Page.SurfaceWidth - ImgW) div 2;
  Img.Top := 0;
  Img.Bitmap.LoadFromFile(ExpandConstant('{tmp}\feature_' + IntToStr(I) + '.bmp'));
end;

procedure InitializeWizard();
var
  I: Integer;
begin
  Upgrading := IsUpgrade();
  AddFeaturePage(1, wpWelcome);
  for I := 2 to FEATURE_COUNT do
    AddFeaturePage(I, FeaturePages[I - 1].ID);

  ClaudePage := CreateInputOptionPage(wpSelectTasks, 'AI 연결',
    'Claude가 PhonePilot을 쓸 수 있게 연결합니다.',
    'Claude Desktop 설정 파일에 PhonePilot을 MCP 서버로 등록합니다. 기존 설정은 그대로 두고 항목만 추가하며, 원래 파일은 .bak으로 보관합니다. ' +
    'Claude Code는 설치 후 README의 안내대로 연결하세요.', False, False);
  ClaudePage.Add('Claude Desktop에 PhonePilot 연결');
  ClaudePage.Values[0] := DirExists(ExpandConstant('{userappdata}\Claude'));

  KeyPage := CreateInputQueryPage(ClaudePage.ID, '내장 AI 에이전트 (선택)',
    'Anthropic API 키를 넣으면 앱 안의 에이전트 패널에서 자연어로 작업을 시킬 수 있습니다.',
    '비워 두고 나중에 %APPDATA%\PhonePilot\.env 의 ANTHROPIC_API_KEY 에 넣어도 됩니다. 키는 이 PC의 사용자 폴더에만 저장됩니다.');
  KeyPage.Add('Anthropic API 키:', True);

  PhonePage := CreateOutputMsgPage(KeyPage.ID, '폰 준비', '설치가 끝나기 전에 폰에서 다음을 준비해 두세요.',
    '1. 설정 → 휴대전화 정보 → 소프트웨어 정보 → [빌드 번호]를 7번 누르면 개발자 옵션이 켜집니다.' + #13#10 +
    '2. 설정 → 개발자 옵션 → [USB 디버깅]을 켭니다.' + #13#10 +
    '3. USB 케이블로 PC에 연결하고, 폰에 뜨는 "USB 디버깅을 허용하시겠습니까?"에서 [이 컴퓨터에서 항상 허용]을 체크하고 [허용]을 누릅니다.' + #13#10 + #13#10 +
    '무선으로 쓰려면(Android 11 이상) 앱의 [기기] 탭 → 무선 연결에서 QR로 페어링하세요.' + #13#10 +
    '허용을 누르면 PhonePilot이 자동으로 연결합니다.');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
var
  I: Integer;
begin
  Result := False;
  if not Upgrading then
    Exit;
  { an update keeps the existing settings: no tour, no setup questions }
  for I := 1 to FEATURE_COUNT do
    if PageID = FeaturePages[I].ID then
      Result := True;
  if (PageID = ClaudePage.ID) or (PageID = KeyPage.ID) or (PageID = PhonePage.ID) then
    Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Key: String;
begin
  Result := True;
  if CurPageID = KeyPage.ID then
  begin
    Key := Trim(KeyPage.Values[0]);
    if (Key <> '') and (Pos('sk-ant-', Key) <> 1) then
      Result := MsgBox('Anthropic API 키는 보통 sk-ant- 로 시작합니다. 이대로 진행할까요?', mbConfirmation, MB_YESNO) = IDYES;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Adb: String;
  Rc: Integer;
begin
  Result := '';
  { the adb server runs from the install folder and would lock adb.exe during an update }
  Adb := ExpandConstant('{app}\_internal\vendor\platform-tools\adb.exe');
  if FileExists(Adb) then
    Exec(Adb, 'kill-server', '', SW_HIDE, ewWaitUntilTerminated, Rc);
end;

procedure RunCli(const Params, Status: String);
var
  Rc: Integer;
begin
  WizardForm.StatusLabel.Caption := Status;
  if not Exec(ExpandConstant('{app}\{#CliExe}'), Params, '', SW_HIDE, ewWaitUntilTerminated, Rc) or (Rc <> 0) then
    Log('phonepilot-cli ' + Params + ' failed: ' + IntToStr(Rc));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  KeyFile, Key: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  RunCli('init', '사용자 설정 파일을 만드는 중…');  { keeps existing files }
  { updates and unattended installs never touch another app's config or the user's secrets }
  if Upgrading or WizardSilent then
    Exit;
  Key := Trim(KeyPage.Values[0]);
  if Key <> '' then
  begin
    { the key goes through a file in Setup's private temp folder, never the command line }
    KeyFile := ExpandConstant('{tmp}\anthropic_key.txt');
    SaveStringToFile(KeyFile, Key, False);
    RunCli('set-secret ANTHROPIC_API_KEY --from-file "' + KeyFile + '"', 'API 키를 저장하는 중…');
    DeleteFile(KeyFile);
  end;
  if ClaudePage.Values[0] then
    RunCli('setup-claude', 'Claude Desktop에 연결하는 중…');
end;
