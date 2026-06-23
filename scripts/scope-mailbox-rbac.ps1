<#
  scope-mailbox-rbac.ps1
  Scopes the OWNDAYS-EOD-Mail-Reader app to a SINGLE mailbox using
  RBAC for Applications in Exchange Online (replaces the legacy Application Access Policy).

  Run as a member of Organization Management / Exchange Administrator.
  Modules are auto-installed (CurrentUser scope) if missing.

  See docs/gmail-to-m365-migration-plan.md section 2 for context.
#>

$ErrorActionPreference = 'Stop'

# ---- Values for this app (pre-filled) -------------------------------------
$AppId       = 'f8964de1-4fdb-4965-8944-c235e5066e74'   # Application (client) ID
$Mailbox     = 'owndaysau.sales@bluebellgroup.com'      # the central EOD mailbox
$DisplayName = 'OWNDAYS EOD Mail Reader'
$ScopeName   = 'EOD Central Mailbox'
$RoleName    = 'Application Mail.ReadWrite'             # scoped Graph-equivalent role

function Ensure-Module([string]$Name) {
    if (-not (Get-Module -ListAvailable -Name $Name)) {
        Write-Host "Installing module $Name (CurrentUser)..." -ForegroundColor Cyan
        Install-Module $Name -Scope CurrentUser -Force -AllowClobber
    }
    Import-Module $Name
}

# ---- 1. Resolve the Entra service principal ObjectId ----------------------
# Object ID of the app under "Enterprise applications" (NOT App registrations).
$SpObjectId = ''
if (-not $SpObjectId) {
    Ensure-Module 'Microsoft.Graph.Applications'
    if (-not (Get-MgContext)) { Connect-MgGraph -Scopes 'Application.Read.All' -NoWelcome }
    $SpObjectId = (Get-MgServicePrincipal -Filter "appId eq '$AppId'").Id
}
if (-not $SpObjectId) { throw "Could not resolve the service principal ObjectId for appId $AppId." }
Write-Host "Service principal ObjectId: $SpObjectId" -ForegroundColor Cyan

# ---- 2. Connect to Exchange Online ----------------------------------------
Ensure-Module 'ExchangeOnlineManagement'
Connect-ExchangeOnline -ShowBanner:$false

# ---- 3. Create the Exchange service-principal pointer (idempotent) --------
$existingSp = Get-ServicePrincipal -ErrorAction SilentlyContinue | Where-Object { $_.AppId -eq $AppId }
if ($existingSp) {
    Write-Host "Exchange service principal already exists (ok)." -ForegroundColor Yellow
} else {
    New-ServicePrincipal -AppId $AppId -ObjectId $SpObjectId -DisplayName $DisplayName | Out-Null
    Write-Host "Created Exchange service principal." -ForegroundColor Green
}

# ---- 4. Create the management scope = ONLY the central mailbox ------------
if (Get-ManagementScope -Identity $ScopeName -ErrorAction SilentlyContinue) {
    Write-Host "Management scope '$ScopeName' already exists (ok)." -ForegroundColor Yellow
} else {
    New-ManagementScope -Name $ScopeName `
        -RecipientRestrictionFilter "PrimarySmtpAddress -eq '$Mailbox'" | Out-Null
    Write-Host "Created management scope '$ScopeName'." -ForegroundColor Green
}

# ---- 5. Assign the scoped Application Mail.ReadWrite role ------------------
$existingAssignment = Get-ManagementRoleAssignment -RoleAssignee $SpObjectId -ErrorAction SilentlyContinue |
    Where-Object { $_.Role -eq $RoleName -and $_.CustomResourceScope -eq $ScopeName }
if ($existingAssignment) {
    Write-Host "Role assignment already exists (ok)." -ForegroundColor Yellow
} else {
    New-ManagementRoleAssignment -App $SpObjectId -Role $RoleName -CustomResourceScope $ScopeName | Out-Null
    Write-Host "Assigned '$RoleName' scoped to '$ScopeName'." -ForegroundColor Green
}

# ---- 6. Verify (this cmdlet bypasses the permission cache) -----------------
Write-Host "`nVerification - expect the central mailbox to show Granted = True:" -ForegroundColor Cyan
Test-ServicePrincipalAuthorization -Identity $SpObjectId -Resource $Mailbox | Format-Table

Write-Host "`nNOTE: RBAC changes take 30 min - 2 h to fully propagate." -ForegroundColor Yellow
Write-Host "Next: confirm the app still reads the mailbox, THEN remove the org-wide" -ForegroundColor Yellow
Write-Host "Entra Mail.ReadWrite consent, then re-verify (migration plan section 2)." -ForegroundColor Yellow
