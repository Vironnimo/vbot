# The tray supplies UTF-8 JSON on stdin. Release text is never evaluated.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$reader = [IO.StreamReader]::new([Console]::OpenStandardInput(), [Text.Encoding]::UTF8)
$notice = $reader.ReadToEnd() | ConvertFrom-Json
$form = [Windows.Forms.Form]::new()
$form.Text = "Before updating to vBot $($notice.version)"
$form.StartPosition = 'CenterScreen'
$form.ClientSize = [Drawing.Size]::new(600, 400)
$form.MinimumSize = [Drawing.Size]::new(400, 280)
$form.Font = [Drawing.SystemFonts]::MessageBoxFont
$body = [Windows.Forms.TextBox]::new()
$body.Multiline = $true
$body.ReadOnly = $true
$body.ScrollBars = 'Vertical'
$body.Dock = 'Fill'
$body.Text = $notice.text -replace '\r?\n', [Environment]::NewLine
$buttons = [Windows.Forms.FlowLayoutPanel]::new()
$buttons.Dock = 'Bottom'
$buttons.FlowDirection = 'RightToLeft'
$buttons.AutoSize = $true
$cancel = [Windows.Forms.Button]::new()
$cancel.Text = 'Cancel'
$cancel.DialogResult = 'Cancel'
$update = [Windows.Forms.Button]::new()
$update.Text = 'Update'
$update.DialogResult = 'OK'
$link = [Windows.Forms.LinkLabel]::new()
$link.Text = "What's new"
$link.AutoSize = $true
$link.Add_LinkClicked({ [Diagnostics.Process]::Start([string]$notice.url) | Out-Null })
$buttons.Controls.AddRange(@($cancel, $update, $link))
$form.Controls.AddRange(@($body, $buttons))
$form.CancelButton = $cancel
$form.AcceptButton = $cancel
$form.ActiveControl = $cancel
try {
    if ($form.ShowDialog() -eq 'OK') { exit 10 }
} finally {
    $form.Dispose()
}
exit 0
