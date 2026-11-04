param(
    [Parameter(Mandatory)][string]$TextPath,
    [Parameter(Mandatory)][string]$WavPath
)
$ErrorActionPreference = 'Stop'
$speechText = Get-Content -LiteralPath $TextPath -Raw -Encoding utf8
$voice = New-Object -ComObject SAPI.SpVoice
$englishVoices = $voice.GetVoices('Language=409')
if ($englishVoices.Count -eq 0) { throw 'No English SAPI voice is installed.' }
$voice.Voice = $englishVoices.Item(0)
$voice.Rate = 0
$voice.Volume = 100
$stream = New-Object -ComObject SAPI.SpFileStream
try {
    $stream.Open([IO.Path]::GetFullPath($WavPath), 3, $false)
    $voice.AudioOutputStream = $stream
    $null = $voice.Speak($speechText)
} finally {
    $stream.Close()
}
Get-Item -LiteralPath $WavPath | Select-Object FullName, Length | ConvertTo-Json -Compress
