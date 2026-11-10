import base64
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave


PWSH=shutil.which('pwsh.exe') or shutil.which('pwsh')
SCRIPT=Path(__file__).resolve().parent/'skill/scripts/make-speech.ps1'
WRAPPER=r'''
$ErrorActionPreference = 'Stop'
function New-Object([string]$ComObject) {
    [IO.File]::WriteAllText($env:UC_TTS_MARKER, 'called')
    if ($ComObject -eq 'SAPI.SpFileStream') {
        $stream = [pscustomobject]@{Path=$null}
        $stream | Add-Member ScriptMethod Open {
            param($path,$mode,$async)
            $this.Path=$path
            [IO.File]::WriteAllBytes($path,[Convert]::FromBase64String($env:UC_TTS_EMPTY))
        }
        $stream | Add-Member ScriptMethod Close {}
        return $stream
    }
    $voice = [pscustomobject]@{Voice=$null; Rate=0; Volume=0; AudioOutputStream=$null}
    $voice | Add-Member ScriptMethod GetVoices {
        param($language)
        if ($language -ne 'Language=409') { throw 'Expected an English voice' }
        $voices=[pscustomobject]@{Count=1}
        $voices | Add-Member ScriptMethod Item { param($index) return 'Synthetic English' }
        return $voices
    }
    $voice | Add-Member ScriptMethod Speak {
        param($text)
        if ($env:UC_TTS_MODE -ne 'sapi') { throw [UnauthorizedAccessException]::new('Synthetic SAPI denial') }
        [IO.File]::WriteAllBytes($this.AudioOutputStream.Path,[Convert]::FromBase64String($env:UC_TTS_VALID))
        return 0
    }
    return $voice
}
$initial=(Get-Location).ProviderPath
try { & $env:UC_TTS_SCRIPT -TextPath $env:UC_TTS_TEXT -WavPath $env:UC_TTS_OUTPUT }
finally { if ((Get-Location).ProviderPath -ne $initial) { throw 'Caller location changed' } }
'''
FFMPEG=r'''
$ErrorActionPreference='Stop'
$index=[Array]::IndexOf($args,'-i')
if ($index -lt 0 -or $args[$index+1] -notmatch '^flite=textfile=([a-zA-Z0-9_.-]+):voice=slt$') { throw 'Unsafe filter path' }
$text=Join-Path (Get-Location).ProviderPath $Matches[1]
if ([IO.File]::ReadAllText($text) -ne $env:UC_TTS_EXPECTED) { throw 'Input text was changed' }
$bytes=[Convert]::FromBase64String($env:UC_TTS_VALID)
if ($env:UC_TTS_MODE -eq 'empty') { $bytes=[Convert]::FromBase64String($env:UC_TTS_EMPTY) }
if ($env:UC_TTS_MODE -eq 'truncated') { $bytes=$bytes[0..44] }
[IO.File]::WriteAllBytes($args[-1],$bytes)
if ($env:UC_TTS_MODE -eq 'race') { [IO.File]::WriteAllText($env:UC_TTS_OUTPUT,'existing destination') }
$global:LASTEXITCODE=0
if ($env:UC_TTS_MODE -eq 'exit') { $global:LASTEXITCODE=9 }
'''


@unittest.skipUnless(PWSH,'PowerShell 7 is required')
class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='ucampus-tts-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"folder with ' quote"
        self.root.mkdir()
        self.text=self.root/"reader's text.txt";self.target=self.root/"reader's output.wav"
        content="A reader's local voice test: quotes and $(literal text)."
        self.text.write_text(content,encoding='utf-8')
        self.wrapper=self.root/'wrapper.ps1';self.wrapper.write_text(WRAPPER,encoding='utf-8')
        self.ffmpeg=self.root/"fake ffmpeg's.ps1";self.ffmpeg.write_text(FFMPEG,encoding='utf-8')
        output=io.BytesIO()
        with wave.open(output,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            wav.writeframes(struct.pack('<h',1000)*1600)
        empty=b'RIFF'+struct.pack('<I',38)+b'WAVEfmt '+struct.pack('<IHHIIHHH',18,1,1,16000,32000,2,16,0)+b'data'+struct.pack('<I',0)
        self.env={**os.environ,'UC_TTS_SCRIPT':str(SCRIPT),'UC_TTS_TEXT':str(self.text),'UC_TTS_OUTPUT':str(self.target),
                  'UC_TTS_MARKER':str(self.root/'sapi-called'),'UC_TTS_EXPECTED':content,
                  'UC_TTS_VALID':base64.b64encode(output.getvalue()).decode(),'UC_TTS_EMPTY':base64.b64encode(empty).decode(),
                  'UCAMPUS_FFMPEG':str(self.ffmpeg)}

    def run_script(self,mode):
        return subprocess.run([PWSH,'-NoProfile','-File',str(self.wrapper)],env={**self.env,'UC_TTS_MODE':mode},
                              capture_output=True,text=True,encoding='utf-8',timeout=20)

    def assert_clean(self):self.assertEqual(list(self.root.glob('.ucampus-speech-*')),[])

    def test_sapi_success_does_not_need_ffmpeg(self):
        self.env['UCAMPUS_FFMPEG']=str(self.root/'absent.exe')
        process=self.run_script('sapi')
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertEqual(json.loads(process.stdout)['backend'],'sapi');self.assert_clean()

    def test_denied_sapi_uses_a_text_file_with_quoted_paths(self):
        process=self.run_script('fallback')
        self.assertEqual(process.returncode,0,process.stderr)
        result=json.loads(process.stdout)
        self.assertEqual(result['backend'],'ffmpeg-libflite');self.assertEqual(result['FullName'],str(self.target))
        with wave.open(str(self.target),'rb') as wav:self.assertEqual(wav.getnframes(),1600)
        self.assert_clean()

    def test_failed_empty_or_truncated_fallback_never_publishes(self):
        for mode in ('exit','empty','truncated'):
            with self.subTest(mode=mode):
                process=self.run_script(mode)
                self.assertNotEqual(process.returncode,0)
                self.assertFalse(self.target.exists());self.assert_clean()

    def test_existing_destination_is_untouched_before_synthesis(self):
        self.target.write_bytes(b'existing destination')
        process=self.run_script('fallback')
        self.assertNotEqual(process.returncode,0)
        self.assertEqual(self.target.read_bytes(),b'existing destination')
        self.assertFalse((self.root/'sapi-called').exists());self.assert_clean()

    def test_destination_created_during_synthesis_is_not_overwritten(self):
        process=self.run_script('race')
        self.assertNotEqual(process.returncode,0)
        self.assertEqual(self.target.read_bytes(),b'existing destination');self.assert_clean()


if __name__=='__main__':unittest.main()
