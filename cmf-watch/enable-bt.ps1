$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $type) {
  $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}
[Windows.Devices.Radios.Radio, Windows.System.Devices, ContentType = WindowsRuntime] | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
foreach ($r in $radios) {
  Write-Output ("{0} | {1} | {2}" -f $r.Kind, $r.Name, $r.State)
  if ($r.Kind -eq 'Bluetooth' -and $r.State -ne 'On') {
    $status = Await ($r.SetStateAsync('On')) ([Windows.Devices.Radios.RadioAccessStatus])
    Write-Output ("SetStateAsync -> {0}" -f $status)
  }
}
