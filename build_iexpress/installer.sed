[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=1
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=
FinishMessage=Installation complete. Use the Desktop shortcut to start the tool.
TargetName=C:\Users\SVINAYAK\Desktop\python_project\dist\ASCF_Compare_Tool_Setup.exe
FriendlyName=ASCF Compare Tool Setup
AppLaunched=setup.cmd
PostInstallCmd=<None>
AdminQuietInst=
UserQuietInst=
SourceFiles=SourceFiles
[Strings]
InstallPrompt=Install ASCF Compare Tool on this PC (localhost only)?
[SourceFiles]
SourceFiles0=C:\Users\SVINAYAK\Desktop\python_project\build_iexpress\
[SourceFiles0]
%FILE0%=setup.cmd
%FILE1%=payload.zip
