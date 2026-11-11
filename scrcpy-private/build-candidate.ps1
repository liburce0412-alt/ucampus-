param(
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$Jdk = $env:JAVA_HOME
)
$ErrorActionPreference = 'Stop'
if (-not $Jdk) { $Jdk = Split-Path -Parent (Split-Path -Parent (Get-Command javac.exe).Source) }
$source = Join-Path $PSScriptRoot 'source\server\src\main'
$build = Join-Path $PSScriptRoot 'build'
$classes = Join-Path $build 'classes'
$gen = Join-Path $build 'gen'
$androidJar = Join-Path $Sdk 'platforms\android-36\android.jar'
$framework = Join-Path $Sdk 'platforms\android-36\framework.aidl'
$buildTools = Join-Path $Sdk 'build-tools\36.0.0'
$server = Join-Path $PSScriptRoot 'scrcpy-server-v5.0.1-private'
if ((Test-Path -LiteralPath $build) -or (Test-Path -LiteralPath $server)) { throw 'Use a fresh candidate build directory' }
New-Item -ItemType Directory -Path $classes,(Join-Path $gen 'com\genymobile\scrcpy') -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $gen 'com\genymobile\scrcpy\BuildConfig.java'),'package com.genymobile.scrcpy; public final class BuildConfig { public static final boolean DEBUG = false; public static final String VERSION_NAME = "5.0.1"; }',[Text.UTF8Encoding]::new($false))
& "$buildTools\aidl.exe" "-o$gen" "-I$source\aidl" "$source\aidl\android\content\IOnPrimaryClipChangedListener.aidl"
if ($LASTEXITCODE) { throw 'Clipboard AIDL generation failed' }
& "$buildTools\aidl.exe" "-o$gen" "-I$source\aidl" -p $framework "$source\aidl\android\view\IDisplayWindowListener.aidl"
if ($LASTEXITCODE) { throw 'Display AIDL generation failed' }
$javaFiles = @((Get-ChildItem -LiteralPath "$source\java" -Filter '*.java' -File -Recurse).FullName) + @((Get-ChildItem -LiteralPath $gen -Filter '*.java' -File -Recurse).FullName)
$javaArguments = @('-encoding','UTF-8','-bootclasspath',$androidJar,'-cp',"$buildTools\core-lambda-stubs.jar;$gen",'-d',$classes,'-source','1.8','-target','1.8','-Xlint:-options') + $javaFiles
$javaArgfile = Join-Path $build 'javac.args'
[IO.File]::WriteAllLines($javaArgfile,@($javaArguments | ForEach-Object { '"' + $_.Replace('\','/') + '"' }),[Text.UTF8Encoding]::new($false))
& "$Jdk\bin\javac.exe" "@$javaArgfile"
if ($LASTEXITCODE) { throw 'Server compilation failed' }
$dexZip = Join-Path $build 'classes.zip'
$dexArguments = @('--classpath',$androidJar,'--output',$dexZip) + @((Get-ChildItem -LiteralPath $classes -Filter '*.class' -File -Recurse).FullName)
$dexArgfile = Join-Path $build 'd8.args'
[IO.File]::WriteAllLines($dexArgfile,@($dexArguments | ForEach-Object { $_.Replace('\','/') }),[Text.UTF8Encoding]::new($false))
& "$Jdk\bin\java.exe" -cp "$buildTools\lib\d8.jar" com.android.tools.r8.D8 "@$dexArgfile"
if ($LASTEXITCODE) { throw 'DEX generation failed' }
[IO.File]::Move($dexZip,$server)
Get-Item -LiteralPath $server | Select-Object FullName,Length
