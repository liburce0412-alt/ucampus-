param(
    [Parameter(Mandatory)][string]$TextPath,
    [Parameter(Mandatory)][string]$WavPath
)
$ErrorActionPreference = 'Stop'

function Test-PcmWave([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return $false }
    $reader = [IO.BinaryReader]::new([IO.File]::OpenRead($Path))
    try {
        if ($reader.BaseStream.Length -lt 44 -or [Text.Encoding]::ASCII.GetString($reader.ReadBytes(4)) -ne 'RIFF') { return $false }
        $end = [long]$reader.ReadUInt32() + 8
        if ($end -ne $reader.BaseStream.Length -or [Text.Encoding]::ASCII.GetString($reader.ReadBytes(4)) -ne 'WAVE') { return $false }
        $formatFound = $false; $dataFound = $false; $blockAlign = 0
        while ($reader.BaseStream.Position + 8 -le $end) {
            $kind = [Text.Encoding]::ASCII.GetString($reader.ReadBytes(4))
            $size = [long]$reader.ReadUInt32()
            $next = $reader.BaseStream.Position + $size + ($size % 2)
            if ($next -gt $end) { return $false }
            if ($kind -eq 'fmt ') {
                if ($formatFound -or $size -lt 16) { return $false }
                $format = $reader.ReadUInt16(); $channels = $reader.ReadUInt16()
                $rate = $reader.ReadUInt32(); $byteRate = $reader.ReadUInt32()
                $blockAlign = $reader.ReadUInt16(); $bits = $reader.ReadUInt16()
                if ($format -ne 1 -or $channels -notin @(1,2) -or $bits -ne 16 -or $rate -lt 8000 -or $rate -gt 96000 -or
                    $blockAlign -ne $channels * 2 -or $byteRate -ne $rate * $blockAlign) { return $false }
                $formatFound = $true
            } elseif ($kind -eq 'data') {
                if (-not $formatFound -or $dataFound -or $size -eq 0 -or $size % $blockAlign -ne 0) { return $false }
                $dataFound = $true
            }
            $reader.BaseStream.Position = $next
        }
        return $formatFound -and $dataFound -and $reader.BaseStream.Position -eq $end
    } catch { return $false }
    finally { $reader.Dispose() }
}

$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WavPath)
if (Test-Path -LiteralPath $destination) { throw 'The destination WAV already exists; inspect it instead of overwriting.' }
$speechText = Get-Content -LiteralPath $TextPath -Raw -Encoding utf8
if ([string]::IsNullOrWhiteSpace($speechText)) { throw 'Speech text is empty.' }
$directory = [IO.Path]::GetDirectoryName($destination)
if (-not [IO.Directory]::Exists($directory)) { throw 'The destination directory must already exist.' }
$temporaryName = '.ucampus-speech-' + [guid]::NewGuid().ToString('N')
$temporaryWav = Join-Path $directory ($temporaryName + '.wav')
$temporaryText = Join-Path $directory ($temporaryName + '.txt')
$owned = @()
try {
    [IO.File]::Open($temporaryWav, [IO.FileMode]::CreateNew).Dispose(); $owned += $temporaryWav
    $voice = $stream = $null; $sapiReady = $false; $sapiFailure = 'Empty or invalid PCM output'
    try {
        $voice = New-Object -ComObject SAPI.SpVoice
        $englishVoices = $voice.GetVoices('Language=409')
        if ($englishVoices.Count -eq 0) { throw 'No English SAPI voice is installed.' }
        $voice.Voice = $englishVoices.Item(0)
        $voice.Rate = 0; $voice.Volume = 100
        $stream = New-Object -ComObject SAPI.SpFileStream
        $stream.Open($temporaryWav, 3, $false)
        $voice.AudioOutputStream = $stream
        $null = $voice.Speak($speechText)
        $stream.Close(); $sapiReady = $true
    } catch { $sapiFailure = $_.Exception.Message }
    finally {
        if ($stream) { try { $stream.Close() } catch {} }
        foreach ($object in @($stream,$voice)) {
            if ($null -ne $object -and [Runtime.InteropServices.Marshal]::IsComObject($object)) {
                $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($object)
            }
        }
    }
    $backend = 'sapi'
    if (-not $sapiReady -or -not (Test-PcmWave $temporaryWav)) {
        $ffmpeg = $env:UCAMPUS_FFMPEG
        if ([string]::IsNullOrWhiteSpace($ffmpeg)) {
            $command = Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue
            if ($command) { $ffmpeg = $command.Source }
        }
        if ([string]::IsNullOrWhiteSpace($ffmpeg) -or -not (Test-Path -LiteralPath $ffmpeg -PathType Leaf)) {
            throw "SAPI did not produce valid PCM ($sapiFailure). Set UCAMPUS_FFMPEG to an installed FFmpeg with libflite."
        }
        $ffmpeg = (Resolve-Path -LiteralPath $ffmpeg).Path
        [IO.File]::Open($temporaryText, [IO.FileMode]::CreateNew).Dispose(); $owned += $temporaryText
        [IO.File]::WriteAllText($temporaryText, $speechText, [Text.UTF8Encoding]::new($false))
        # A safe relative filename avoids libavfilter's separate quoting rules for Windows paths.
        Push-Location -LiteralPath $directory
        try {
            $filter = 'flite=textfile=' + [IO.Path]::GetFileName($temporaryText) + ':voice=slt'
            & $ffmpeg -hide_banner -loglevel error -nostdin -f lavfi -i $filter -ac 1 -ar 16000 -c:a pcm_s16le -y $temporaryWav
            if ($LASTEXITCODE -ne 0) { throw "FFmpeg speech synthesis failed with exit code $LASTEXITCODE." }
        } finally { Pop-Location }
        if (-not (Test-PcmWave $temporaryWav)) { throw 'FFmpeg did not produce nonempty valid PCM16 WAV.' }
        $backend = 'ffmpeg-libflite'
    }
    # Move refuses an existing destination, including one created while synthesis was running.
    [IO.File]::Move($temporaryWav, $destination)
    [pscustomobject]@{FullName=$destination; Length=(Get-Item -LiteralPath $destination).Length; backend=$backend} | ConvertTo-Json -Compress
} finally {
    foreach ($path in $owned) { if ([IO.File]::Exists($path)) { Remove-Item -LiteralPath $path -Force } }
}
