param(
    [Parameter(Mandatory = $true)][string]$ScriptInput,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)
$ErrorActionPreference = "Stop"
$packet = Get-Content -LiteralPath $ScriptInput -Raw -Encoding UTF8 | ConvertFrom-Json
$speechDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $speechDirectory -PathType Container)) {
    throw "The caller must create the isolated speech output directory first."
}
$hearingVoice = New-Object -ComObject SAPI.SpVoice
$hearingVoiceTokens = $hearingVoice.GetVoices()
$usedVoices = @{}
try {
    foreach ($recording in $packet.recordings) {
        foreach ($turn in $recording.turns) {
            if ($turn.id -notmatch '^[a-z][a-z0-9-]{1,90}$') {
                throw "Unsafe speech turn identifier."
            }
            $voiceProperty = $recording.voice_map.PSObject.Properties[$turn.speaker]
            if ($null -eq $voiceProperty -or [string]::IsNullOrWhiteSpace($turn.text)) {
                throw "Every speech turn needs a mapped voice and nonempty text."
            }
            $wantedVoice = [string]$voiceProperty.Value
            $selectedVoice = $null
            for ($voiceIndex = 0; $voiceIndex -lt $hearingVoiceTokens.Count; $voiceIndex++) {
                $candidateVoice = $hearingVoiceTokens.Item($voiceIndex)
                if ($candidateVoice.GetDescription().StartsWith($wantedVoice)) {
                    $selectedVoice = $candidateVoice
                    break
                }
            }
            if ($null -eq $selectedVoice) { throw "Required local voice missing: $wantedVoice" }
            $hearingVoice.Voice = $selectedVoice
            $hearingVoice.Rate = 0
            $hearingVoice.Volume = 100
            $usedVoices[$wantedVoice] = $selectedVoice.GetDescription()
            $speechFile = Join-Path $speechDirectory ($turn.id + ".wav")
            $speechStream = New-Object -ComObject SAPI.SpFileStream
            try {
                $speechStream.Format.Type = 18 # SAFT16kHz16BitMono
                $speechStream.Open($speechFile, 3, $false) # SSFMCreateForWrite
                $hearingVoice.AllowAudioOutputFormatChangesOnNextSet = $false
                $hearingVoice.AudioOutputStream = $speechStream
                $null = $hearingVoice.Speak([string]$turn.text, 16) # synchronous, literal text
            } finally {
                $speechStream.Close()
                [Runtime.InteropServices.Marshal]::ReleaseComObject($speechStream) | Out-Null
            }
        }
    }
    $usedVoices | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $speechDirectory "voices.json") -Encoding UTF8
} finally {
    [Runtime.InteropServices.Marshal]::ReleaseComObject($hearingVoice) | Out-Null
}
