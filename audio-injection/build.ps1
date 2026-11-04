param(
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$Jdk = $env:JAVA_HOME
)
$ErrorActionPreference = 'Stop'
if (-not $Jdk) {
    $javac = Get-Command javac.exe -ErrorAction SilentlyContinue
    if (-not $javac) { throw 'Set JAVA_HOME or pass -Jdk to an existing JDK.' }
    $Jdk = Split-Path -Parent (Split-Path -Parent $javac.Source)
}
$probeBuild = Join-Path $PSScriptRoot 'build'
$androidJar = Join-Path $Sdk 'platforms\android-36\android.jar'
New-Item -ItemType Directory -Force -Path "$probeBuild\classes", "$probeBuild\dex" | Out-Null
& "$Jdk\bin\javac.exe" -encoding UTF-8 -source 8 -target 8 -Xlint:-options -classpath $androidJar -d "$probeBuild\classes" "$PSScriptRoot\src\local\ucampus\audio\InjectionProbe.java"
if ($LASTEXITCODE) { throw 'Probe compilation failed' }
& "$Jdk\bin\jar.exe" --create --file "$probeBuild\classes.jar" -C "$probeBuild\classes" .
if ($LASTEXITCODE) { throw 'Class packaging failed' }
& "$Sdk\build-tools\36.0.0\d8.bat" --lib $androidJar --min-api 31 --output "$probeBuild\dex" "$probeBuild\classes.jar"
if ($LASTEXITCODE) { throw 'DEX compilation failed' }
& "$Jdk\bin\jar.exe" --create --file "$PSScriptRoot\ucampus-audio-probe.jar" -C "$probeBuild\dex" classes.dex
if ($LASTEXITCODE) { throw 'Probe packaging failed' }
Get-Item -LiteralPath "$PSScriptRoot\ucampus-audio-probe.jar" | Select-Object FullName,Length
