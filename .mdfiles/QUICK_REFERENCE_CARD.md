# ASCF Comparison Tool - Quick Reference Card

## 🚀 Quick Start Commands

### Start Server
```powershell
cd C:\PythonProject_UPDATED\python_project
$env:CFR_JAR = "C:\cfr-0.152.jar"
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

### Full Restart (Kill + Clear Cache + Start)
```powershell
$env:CFR_JAR = "C:\cfr-0.152.jar"
taskkill /F /PID (netstat -ano | Select-String ":5000.*LISTENING" | ForEach-Object { ($_ -split '\s+')[-1] }) 2>$null
Remove-Item -Recurse -Force __pycache__, src\__pycache__ -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
python builtin_server.py --host 0.0.0.0 --port 5000 --skip-demo
```

### Stop Server
```powershell
# Find PID
netstat -ano | findstr ":5000" | findstr "LISTENING"

# Kill by PID
taskkill /F /PID <pid_number>
```

---

## 🌐 Access URLs

| Type | URL |
|------|-----|
| Local | http://localhost:5000 |
| Network | http://192.168.1.41:5000 |

---

## 📊 Comparison Modes

| Mode | Use Case |
|------|----------|
| **Full Content** | Complete file comparison |
| **JAR Naming** | JAR version changes |
| **Archives** | EAR/WAR/JAR/RPM deep scan |
| **FOSS Foundation** | Specific JAR analysis |

---

## 📁 Example Paths

### Network Share (UNC)
```
\\INPNQASCFLB02\uif_10250\uif\ASCF_GA_10250pb00\DU
\\INPNQASCFLB02\uif_10250\uif\ASCF_HF_10250pb00hf01\DU
```

### Local Path
```
C:\Builds\OldBuild
C:\Builds\NewBuild
```

---

## 🔧 Troubleshooting

| Error | Solution |
|-------|----------|
| Access Denied | Run as Admin / Authenticate to share |
| No results | Check paths exist |
| Progress stuck | Restart server |
| Decompile fails | Check CFR_JAR env var |

---

## ⚙️ Environment Variables

```powershell
# Set CFR decompiler path
$env:CFR_JAR = "C:\cfr-0.152.jar"

# Verify
echo $env:CFR_JAR
```

---

## 📝 Report Location

Reports saved to: `C:\PythonProject_UPDATED\python_project\`

Format: `ascf_<mode>_report_<timestamp>.html`

---

*Quick Reference v1.0*




