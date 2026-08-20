# Creates self-signed TLS files: .ssl/server.crt and .ssl/server.key (PEM).
# Uses Windows certificate APIs only (no OpenSSL / no pip).
param(
    [string]$ServerIP = "",
    [string]$OutDir = (Join-Path (Split-Path $PSScriptRoot -Parent) ".ssl")
)
$detectedIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.IPAddress -notmatch '^169\.' } |
    ForEach-Object { $_.IPAddress } | Select-Object -Unique)
if (-not $ServerIP) {
    $ServerIP = ($detectedIps | Select-Object -First 1)
}
if (-not $ServerIP) {
    $ServerIP = "127.0.0.1"
}
$sanParts = @("IPAddress=127.0.0.1")
foreach ($ip in $detectedIps) {
    if ($sanParts -notcontains "IPAddress=$ip") { $sanParts += "IPAddress=$ip" }
}
if ($sanParts -notcontains "IPAddress=$ServerIP") { $sanParts += "IPAddress=$ServerIP" }
$sanText = ($sanParts -join '&')
function Get-DerLengthBytes([int]$len) {
    if ($len -lt 0x80) { return ,[byte]$len }
    $tmp = [System.Collections.Generic.List[byte]]::new()
    $n = $len
    while ($n -gt 0) { $tmp.Insert(0, [byte]($n -band 0xff)); $n = [int]([math]::Floor($n / 256)) }
    $bl = $tmp.Count
    $out = [System.Collections.Generic.List[byte]]::new()
    [void]$out.Add([byte](0x80 + $bl))
    foreach ($b in $tmp) { [void]$out.Add($b) }
    return ,$out.ToArray()
}
function Encode-DerIntegerUnsigned([byte[]]$beUnsigned) {
    if ($null -eq $beUnsigned -or $beUnsigned.Length -eq 0) { $beUnsigned = ,[byte]0 }
    $i = 0
    while ($i -lt $beUnsigned.Length - 1 -and $beUnsigned[$i] -eq 0) { $i++ }
    $v = New-Object byte[] ($beUnsigned.Length - $i)
    [Array]::Copy($beUnsigned, $i, $v, 0, $v.Length)
    if ($v[0] -band 0x80) {
        $padded = New-Object byte[] ($v.Length + 1)
        $padded[0] = 0
        [Array]::Copy($v, 0, $padded, 1, $v.Length)
        $v = $padded
    }
    $lenBytes = Get-DerLengthBytes $v.Length
    $seq = [System.Collections.Generic.List[byte]]::new()
    [void]$seq.Add(0x02)
    foreach ($b in $lenBytes) { [void]$seq.Add($b) }
    foreach ($b in $v) { [void]$seq.Add($b) }
    return ,$seq.ToArray()
}
function Encode-DerSequence([byte[]]$content) {
    $lb = Get-DerLengthBytes $content.Length
    $seq = [System.Collections.Generic.List[byte]]::new()
    [void]$seq.Add(0x30)
    foreach ($b in $lb) { [void]$seq.Add($b) }
    foreach ($b in $content) { [void]$seq.Add($b) }
    return ,$seq.ToArray()
}
function Export-RsaPrivateKeyPkcs1Pem([System.Security.Cryptography.RSAParameters]$p) {
    $parts = [System.Collections.Generic.List[byte]]::new()
    foreach ($segment in @(
            (Encode-DerIntegerUnsigned (,0)),
            (Encode-DerIntegerUnsigned $p.Modulus),
            (Encode-DerIntegerUnsigned $p.Exponent),
            (Encode-DerIntegerUnsigned $p.D),
            (Encode-DerIntegerUnsigned $p.P),
            (Encode-DerIntegerUnsigned $p.Q),
            (Encode-DerIntegerUnsigned $p.DP),
            (Encode-DerIntegerUnsigned $p.DQ),
            (Encode-DerIntegerUnsigned $p.InverseQ)
        )) {
        foreach ($b in $segment) { [void]$parts.Add($b) }
    }
    $rsaPriv = Encode-DerSequence ($parts.ToArray())
    $b64 = [Convert]::ToBase64String($rsaPriv, [System.Base64FormattingOptions]::InsertLineBreaks)
    return "-----BEGIN RSA PRIVATE KEY-----`r`n$b64`r`n-----END RSA PRIVATE KEY-----"
}
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$certPath = Join-Path $OutDir "server.crt"
$keyPath = Join-Path $OutDir "server.key"
$tempPfx = Join-Path $env:TEMP ("dev_https_" + [Guid]::NewGuid().ToString("n") + ".pfx")
$pfxPwdPlain = "dev-temp-export-" + [Guid]::NewGuid().ToString("n")
$cert = $null
try {
    $cert = New-SelfSignedCertificate `
        -Subject "CN=$ServerIP" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -Provider "Microsoft Enhanced RSA and AES Cryptographic Provider" `
        -KeySpec KeyExchange `
        -KeyExportPolicy Exportable `
        -KeyAlgorithm RSA `
        -KeyLength 2048 `
        -HashAlgorithm SHA256 `
        -NotAfter (Get-Date).AddYears(10) `
        -FriendlyName "Folder comparison dev HTTPS" `
        -KeyUsage KeyEncipherment, DigitalSignature `
        -TextExtension @("2.5.29.17={text}$sanText")

    $pfxPwd = ConvertTo-SecureString -String $pfxPwdPlain -AsPlainText -Force
    Export-PfxCertificate -Cert $cert -FilePath $tempPfx -Password $pfxPwd | Out-Null

    $pfxBytes = [System.IO.File]::ReadAllBytes($tempPfx)
    # EphemeralKeySet can block PKCS#8 export; UserKeySet + Exportable allows export.
    $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable -bor `
        [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::UserKeySet -bor `
        [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::PersistKeySet
    $cert2 = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 ($pfxBytes, $pfxPwdPlain, $flags)

    $certDer = $cert2.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert)
    $certB64 = [Convert]::ToBase64String($certDer, [System.Base64FormattingOptions]::InsertLineBreaks)
    @"
-----BEGIN CERTIFICATE-----
$certB64
-----END CERTIFICATE-----
"@ | Set-Content -Path $certPath -Encoding ASCII

    $params = $null
    $legacyPk = $cert2.PrivateKey
    if ($legacyPk -is [System.Security.Cryptography.RSACryptoServiceProvider]) {
        $params = $legacyPk.ExportParameters($true)
    }
    if ($null -eq $params) {
        $rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert2)
        if ($null -ne $rsa) {
            try {
                $params = $rsa.ExportParameters($true)
            }
            catch { }
        }
    }
    if ($null -eq $params) {
        throw "Could not export RSA parameters. Install Git (OpenSSL) or run: py -m pip install cryptography && py `"$PSScriptRoot\generate_https_cert.py`""
    }
    $keyPem = Export-RsaPrivateKeyPkcs1Pem $params
    Set-Content -Path $keyPath -Value $keyPem -Encoding ASCII

    Write-Host "Certificate: $certPath"
    Write-Host "Private key: $keyPath"
    Write-Host ""
    Write-Host "Built-in server (HTTP + HTTPS; open firewall for both ports if needed):"
    Write-Host "  py builtin_server.py --host 0.0.0.0 --port 5000 --ssl-cert `"$certPath`" --ssl-key `"$keyPath`""
    Write-Host "  -> http://<host>:5000/crm  and  https://<host>:5443/crm  (HTTPS port: env HTTPS_PORT or 5443)"
    Write-Host ""
    Write-Host "Flask app:"
    Write-Host "  py web_app.py --host 0.0.0.0 --port 5000 --ssl-cert `"$certPath`" --ssl-key `"$keyPath`""
    Write-Host "  -> same: HTTP on 5000, HTTPS on 5443 by default"
    Write-Host ""
    Write-Host "Optional: set PUBLIC_HOST=$ServerIP so startup prints your URL."
}
finally {
    if (Test-Path $tempPfx) { Remove-Item -Force $tempPfx -ErrorAction SilentlyContinue }
    if ($null -ne $cert -and $cert.Thumbprint) {
        Get-ChildItem "Cert:\CurrentUser\My\$($cert.Thumbprint)" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    }
}
