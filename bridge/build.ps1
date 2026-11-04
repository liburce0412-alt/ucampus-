param(
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$Jdk = $env:JAVA_HOME,
    [string]$OutputApk = (Join-Path $PSScriptRoot 'ucampus-bridge-display-test.apk'),
    [string]$KeyStore = (Join-Path $PSScriptRoot 'test-signing.p12')
)
$ErrorActionPreference = 'Stop'
if (-not $Jdk) {
    $javac = Get-Command javac.exe -ErrorAction SilentlyContinue
    if (-not $javac) { throw 'Set JAVA_HOME or pass -Jdk to an existing JDK.' }
    $Jdk = Split-Path -Parent (Split-Path -Parent $javac.Source)
}
$sourceRoot = $PSScriptRoot
$buildRoot = Join-Path $sourceRoot 'build'
$toolsRoot = Join-Path $Sdk 'build-tools\36.0.0'
$androidJar = Join-Path $Sdk 'platforms\android-36\android.jar'
foreach ($path in @($androidJar, "$toolsRoot\aapt2.exe", "$toolsRoot\d8.bat", "$toolsRoot\apksigner.bat", "$Jdk\bin\javac.exe")) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing existing tool: $path" }
}
New-Item -ItemType Directory -Force -Path $buildRoot, "$buildRoot\classes", "$buildRoot\dex" | Out-Null
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw "Build command failed: $LASTEXITCODE" } }
& "$toolsRoot\aapt2.exe" compile --dir "$sourceRoot\res" -o "$buildRoot\resources.zip"
Check-Exit
& "$toolsRoot\aapt2.exe" link -o "$buildRoot\unsigned.apk" -I $androidJar --manifest "$sourceRoot\AndroidManifest.xml" --min-sdk-version 26 --target-sdk-version 36 "$buildRoot\resources.zip"
Check-Exit
$sources = @(Get-ChildItem -LiteralPath "$sourceRoot\src" -Recurse -Filter '*.java' | ForEach-Object { $_.FullName })
& "$Jdk\bin\javac.exe" -encoding UTF-8 -source 8 -target 8 -Xlint:-options -classpath $androidJar -d "$buildRoot\classes" @sources
Check-Exit
& "$Jdk\bin\jar.exe" --create --file "$buildRoot\classes.jar" -C "$buildRoot\classes" .
Check-Exit
& "$toolsRoot\d8.bat" --lib $androidJar --min-api 26 --output "$buildRoot\dex" "$buildRoot\classes.jar"
Check-Exit
& "$Jdk\bin\jar.exe" --update --file "$buildRoot\unsigned.apk" -C "$buildRoot\dex" classes.dex
Check-Exit
& "$toolsRoot\zipalign.exe" -f 4 "$buildRoot\unsigned.apk" "$buildRoot\aligned.apk"
Check-Exit
$keyPath = $KeyStore
if (-not (Test-Path -LiteralPath $keyPath)) {
    & "$Jdk\bin\keytool.exe" -genkeypair -keystore $keyPath -storetype PKCS12 -storepass android -keypass android -alias local-test -keyalg RSA -keysize 2048 -validity 3650 -dname 'CN=UCampus Local Test'
    Check-Exit
}
& "$toolsRoot\apksigner.bat" sign --ks $keyPath --ks-key-alias local-test --ks-pass pass:android --key-pass pass:android --out $OutputApk "$buildRoot\aligned.apk"
Check-Exit
& "$toolsRoot\apksigner.bat" verify --verbose $OutputApk
Check-Exit
Write-Output "Built locally only: $OutputApk"
