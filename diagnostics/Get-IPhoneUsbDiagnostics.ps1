[CmdletBinding()]
param([string]$OutputPath)
$ErrorActionPreference = 'Stop'
if(-not $OutputPath) { $OutputPath = Join-Path $PSScriptRoot 'latest-usb-diagnostics.json' }
if (-not ('IPhoneUsbProbe' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'UsbHubProbe.cs') }
$devicePropertyCache = @{}
function DeviceProperty($Id, $Key) {
    if(-not $devicePropertyCache.ContainsKey($Id)) {
        $values = @{}
        try { Get-PnpDeviceProperty -InstanceId $Id -ErrorAction Stop | ForEach-Object { $values[$_.KeyName]=$_.Data } } catch { }
        $devicePropertyCache[$Id]=$values
    }
    $devicePropertyCache[$Id][$Key]
}
function IdentityHash($Value, [int]$Length = 12) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-','').Substring(0,$Length).ToLowerInvariant() } finally { $sha.Dispose() }
}
$phones = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^USB\\VID_05AC&PID_' -and $_.InstanceId -notmatch '&MI_' -and $_.FriendlyName -match 'Apple Mobile Device|iPhone|iPad' })
$phoneReports = @()
foreach ($phone in $phones) {
    $parent = DeviceProperty $phone.InstanceId 'DEVPKEY_Device_Parent'
    $port = DeviceProperty $phone.InstanceId 'DEVPKEY_Device_Address'
    $hubResult = if($parent -and $port) { @([IPhoneUsbProbe]::Query($parent, [int]$port)) } else { @() }
    $chain = @()
    $ancestor = $parent
    for($depth=0; $ancestor -and $depth -lt 8; $depth++) {
        $node = Get-PnpDevice -InstanceId $ancestor -ErrorAction SilentlyContinue
        if(-not $node -or (-not $node.Class -and -not $node.FriendlyName)) { break }
        $chain += [ordered]@{ Name=$node.FriendlyName; Class=$node.Class; DriverVersion=(DeviceProperty $ancestor 'DEVPKEY_Device_DriverVersion'); Location=(DeviceProperty $ancestor 'DEVPKEY_Device_LocationInfo') }
        $ancestor = DeviceProperty $ancestor 'DEVPKEY_Device_Parent'
    }
    $serial = ($phone.InstanceId -split '\\')[-1]
    $pairingName = if($serial.Length -eq 24) { $serial.Substring(0,8)+'-'+$serial.Substring(8) } else { $serial }
    $pairingPresent = Test-Path -LiteralPath (Join-Path "$env:ProgramData\Apple\Lockdown" ($pairingName+'.plist'))
    $phoneReports += [ordered]@{
        DeviceKey=(IdentityHash $phone.InstanceId); DeviceId=('iphone-'+(IdentityHash $pairingName 20)); Name=$phone.FriendlyName; Status=$phone.Status
        DriverVersion=(DeviceProperty $phone.InstanceId 'DEVPKEY_Device_DriverVersion')
        DriverProvider=(DeviceProperty $phone.InstanceId 'DEVPKEY_Device_DriverProvider')
        DriverService=(DeviceProperty $phone.InstanceId 'DEVPKEY_Device_Service')
        Location=(DeviceProperty $phone.InstanceId 'DEVPKEY_Device_LocationInfo')
        LocationPaths=@(DeviceProperty $phone.InstanceId 'DEVPKEY_Device_LocationPaths')
        PairingRecordPresent=$pairingPresent; UsbConnection=@($hubResult); Ancestors=$chain
    }
}
$services = @(Get-CimInstance Win32_Service | Where-Object { $_.Name -match 'Apple|Bonjour' -or $_.DisplayName -match 'Apple|Bonjour' } | Select-Object Name,DisplayName,State,StartMode)
$drivers = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $_.DeviceName -match 'Apple|iPhone' } | Select-Object DeviceName,DriverVersion,DriverProviderName,InfName)
$packages = @(Get-AppxPackage *Apple* | Select-Object Name,Version)
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 27015 -ErrorAction SilentlyContinue)
$report = [ordered]@{
    CapturedAtUtc=[DateTime]::UtcNow.ToString('o'); ComputerPlatform='Windows'; Phones=$phoneReports
    AppleServices=$services; AppleDrivers=$drivers; ApplePackages=$packages; UsbMuxTcpListenerPresent=($listeners.Count -gt 0)
    Notes=@('Read-only diagnostics. Pairing record contents are never read. Device identities are SHA-256 fingerprints.', 'USB signaling rate is not file transfer throughput. USB 3 capability and current operating speed are reported separately.', 'The presence of a local pairing record does not validate trust, pairing, or access while the phone is locked.')
}
$json = $report | ConvertTo-Json -Depth 10
$json | Set-Content -LiteralPath $OutputPath -Encoding UTF8
$json
