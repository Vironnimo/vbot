# vBot's PowerShell command analysis helper (core/permissions/commands).
#
# Started once by CommandAnalyzer and kept running: reads one JSON request per
# line ({"id": n, "script": "<base64 UTF-8>"}) and answers one JSON line
# ({"id": n, "ok": true, "result": {...}} or {"id": n, "ok": false, "error": "..."}).
# It only parses: PowerShell's own parser and StaticParameterBinder, alias and
# command lookups. Nothing of the analyzed text ever runs. Module autoloading is
# off and the built-in modules load from $PSHOME, so no module from the user's
# module path runs either.
#
# The result lowers the syntax tree without judging it: every command (also in
# script blocks, subexpressions and pipelines) with its words as templates of
# literal text, variable references and dynamic parts, its bound parameters,
# redirections and pipeline position. Python decides what the words mean.

param([System.IO.TextReader]$Reader)

$ErrorActionPreference = 'Stop'
$PSModuleAutoLoadingPreference = 'None'
Set-StrictMode -Off

foreach ($module in 'Microsoft.PowerShell.Management', 'Microsoft.PowerShell.Utility', 'Microsoft.PowerShell.Security', 'Microsoft.PowerShell.Archive') {
    $location = [System.IO.Path]::Combine($PSHOME, 'Modules', $module)
    if ([System.IO.Directory]::Exists($location)) {
        try { Import-Module -Name $location -ErrorAction Stop } catch { }
    }
}

# Aliases, functions and cmdlets of this session by name. GetCommand takes
# about 25 ms to report a name it does not know, the common case of a program.
$script:Commands = [System.Collections.Generic.Dictionary[string, System.Management.Automation.CommandInfo]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($info in @(Get-Command -CommandType Cmdlet) + @(Get-ChildItem function:) + @(Get-ChildItem alias:)) {
    $script:Commands[$info.Name] = $info
}

function New-List { return , [System.Collections.Generic.List[object]]::new() }

function Get-VariableName($VariableAst) {
    $name = $VariableAst.VariablePath.UserPath.ToLowerInvariant()
    foreach ($scope in 'global:', 'script:', 'local:', 'private:', 'using:') {
        if ($name.StartsWith($scope)) { return $name.Substring($scope.Length) }
    }
    return $name
}

function Add-Text($Parts, [string]$Text) {
    if ($Text.Length -gt 0) { $Parts.Add(@{ k = 's'; v = $Text }) }
}

function ConvertTo-Word($Ast) {
    $word = @{ source = $Ast.Extent.Text; parts = (New-List) }
    switch ($Ast.GetType().Name) {
        'StringConstantExpressionAst' {
            Add-Text $word.parts $Ast.Value
            $word.quoted = ([string]$Ast.StringConstantType -ne 'BareWord')
        }
        'ExpandableStringExpressionAst' {
            # Value keeps each nested expression's text verbatim; the literal
            # rest between them is already unescaped. A literal rest that still
            # holds a '$' could be an escaped reference: then the split is not
            # certain and the word stays dynamic.
            $word.quoted = ([string]$Ast.StringConstantType -ne 'BareWord')
            $rest = $Ast.Value
            foreach ($nested in $Ast.NestedExpressions) {
                $text = $nested.Extent.Text
                $at = $rest.IndexOf($text, [System.StringComparison]::Ordinal)
                if ($at -lt 0) { $word.parts = (New-List); $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }); return $word }
                $before = $rest.Substring(0, $at)
                if ($before.Contains('$')) { $word.parts = (New-List); $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }); return $word }
                Add-Text $word.parts $before
                if ($nested -is [System.Management.Automation.Language.VariableExpressionAst]) {
                    $word.parts.Add(@{ k = 'v'; v = (Get-VariableName $nested) })
                } else {
                    $word.parts.Add(@{ k = 'd'; v = $text })
                }
                $rest = $rest.Substring($at + $text.Length)
            }
            if ($rest.Contains('$')) { $word.parts = (New-List); $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }); return $word }
            Add-Text $word.parts $rest
        }
        'ConstantExpressionAst' { Add-Text $word.parts $Ast.Extent.Text }
        'VariableExpressionAst' {
            if ($Ast.Splatted) { $word.splat = $true; $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }) }
            else { $word.parts.Add(@{ k = 'v'; v = (Get-VariableName $Ast) }) }
        }
        'ArrayLiteralAst' {
            $word.items = (New-List)
            foreach ($element in $Ast.Elements) { $word.items.Add((ConvertTo-Word $element)) }
        }
        'ScriptBlockExpressionAst' { $word.block = $true; $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }) }
        'CommandParameterAst' {
            $word.parameter = $Ast.ParameterName
            if ($null -ne $Ast.Argument) { $word.argument = (ConvertTo-Word $Ast.Argument) }
        }
        default { $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }) }
    }
    return $word
}

function Get-Pipeline($CommandAst) {
    $parent = $CommandAst.Parent
    if ($parent -is [System.Management.Automation.Language.PipelineAst] -and $parent.PipelineElements.Count -gt 1) {
        return @{ id = $parent.Extent.StartOffset; index = $parent.PipelineElements.IndexOf($CommandAst) }
    }
    return $null
}

function Get-Redirections($CommandAst) {
    $redirections = New-List
    foreach ($redirection in $CommandAst.Redirections) {
        if ($redirection -is [System.Management.Automation.Language.FileRedirectionAst]) {
            $redirections.Add(@{
                kind = $(if ($redirection.Append) { 'append' } else { 'write' })
                target = (ConvertTo-Word $redirection.Location)
            })
        }
    }
    return , $redirections
}

function Get-Binding($CommandAst) {
    $bound = @{}
    $unbound = New-List
    $errors = New-List
    $binding = [System.Management.Automation.Language.StaticParameterBinder]::BindCommand($CommandAst, $true)
    foreach ($entry in $binding.BoundParameters.GetEnumerator()) {
        $value = $entry.Value.Value
        if ($entry.Key -match '^\d+$') {
            if ($null -ne $value) { $unbound.Add((ConvertTo-Word $value)) }
        } elseif ($null -eq $value -or $value -is [System.Management.Automation.Language.CommandParameterAst]) {
            $bound[$entry.Key] = $null
        } else {
            $bound[$entry.Key] = (ConvertTo-Word $value)
        }
    }
    foreach ($key in $binding.BindingExceptions.Keys) { $errors.Add([string]$key) }
    return @{ bound = $bound; unbound = $unbound; errors = $errors }
}

function Get-Analysis([string]$Text) {
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$parseErrors)
    $result = @{ errors = (New-List); incomplete = $false; commands = (New-List); assigned = (New-List); functions = (New-List); environment_changed = $false }
    foreach ($parseError in $parseErrors) {
        $result.errors.Add($parseError.Message)
        if ($parseError.IncompleteInput) { $result.incomplete = $true }
    }
    if ($result.errors.Count -gt 0) { return $result }

    $functions = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($definition in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
        if ($functions.Add($definition.Name)) { $result.functions.Add($definition.Name) }
    }
    foreach ($node in $ast.FindAll({ param($node)
                $node -is [System.Management.Automation.Language.AssignmentStatementAst] -or
                $node -is [System.Management.Automation.Language.ForEachStatementAst] -or
                $node -is [System.Management.Automation.Language.ParameterAst] -or
                $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -or
                ($node -is [System.Management.Automation.Language.UnaryExpressionAst] -and
                    ([string]$node.TokenKind -match 'PlusPlus|MinusMinus|PostfixPlusPlus|PostfixMinusMinus'))
            }, $true)) {
        $targets = @()
        if ($node -is [System.Management.Automation.Language.AssignmentStatementAst]) { $targets = @($node.Left) }
        elseif ($node -is [System.Management.Automation.Language.ForEachStatementAst]) { $targets = @($node.Variable) }
        elseif ($node -is [System.Management.Automation.Language.ParameterAst]) { $targets = @($node.Name) }
        elseif ($node -is [System.Management.Automation.Language.UnaryExpressionAst]) { $targets = @($node.Child) }
        else {
            # [Environment]::SetEnvironmentVariable and friends change variables
            # this analysis cannot follow.
            if ([string]$node.Member -match 'EnvironmentVariable') { $result.environment_changed = $true }
            continue
        }
        foreach ($target in $targets) {
            foreach ($variable in $target.FindAll({ param($inner) $inner -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
                $result.assigned.Add((Get-VariableName $variable))
            }
        }
    }

    foreach ($node in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandBaseAst] }, $true)) {
        $entry = @{ offset = $node.Extent.StartOffset; source = $node.Extent.Text; redirects = (Get-Redirections $node); pipeline = (Get-Pipeline $node) }
        if ($node -isnot [System.Management.Automation.Language.CommandAst]) {
            $entry.expression = $true
            $result.commands.Add($entry)
            continue
        }
        $elements = $node.CommandElements
        $entry.invocation = switch ([string]$node.InvocationOperator) { 'Ampersand' { '&' } 'Dot' { '.' } default { '' } }
        $entry.name = (ConvertTo-Word $elements[0])
        $words = New-List
        $stopParsing = $false
        for ($index = 1; $index -lt $elements.Count; $index++) {
            $element = $elements[$index]
            if ($element -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
                [string]$element.StringConstantType -eq 'BareWord' -and $element.Value -eq '--%') { $stopParsing = $true }
            $words.Add((ConvertTo-Word $element))
        }
        $entry.words = $words
        $entry.stop_parsing = $stopParsing
        $name = $node.GetCommandName()
        if ($name) {
            if ($functions.Contains($name)) {
                $entry.script_function = $true
            } else {
                $info = $null
                [void]$script:Commands.TryGetValue($name, [ref]$info)
                if ($info -and [string]$info.CommandType -eq 'Alias') {
                    $entry.alias = $true
                    if ($info.ResolvedCommand) { $info = $info.ResolvedCommand }
                    else { $entry.resolved = $info.Definition; $info = $null }
                }
                if ($info -and ([string]$info.CommandType -eq 'Cmdlet' -or [string]$info.CommandType -eq 'Function')) {
                    $entry.resolved = $info.Name
                    $entry.type = [string]$info.CommandType
                    try { $entry.binding = (Get-Binding $node) } catch { $entry.binding = @{ bound = @{}; unbound = (New-List); errors = (New-List) }; $entry.binding.errors.Add('binder') }
                }
            }
        }
        $result.commands.Add($entry)
    }
    return $result
}

# Warm up: the first analysis compiles the code paths every later one takes.
$warmUp = 'Get-ChildItem -Path x | Remove-Item -Recurse; tool "$env:HOME" > $null'
$null = ConvertTo-Json -Compress -Depth 64 -InputObject (Get-Analysis $warmUp)

$writer = [System.IO.StreamWriter]::new([Console]::OpenStandardOutput(), [System.Text.UTF8Encoding]::new($false))
$writer.WriteLine((ConvertTo-Json -Compress -InputObject @{ ready = $true; version = [string]$PSVersionTable.PSVersion }))
$writer.Flush()
while ($null -ne ($line = $Reader.ReadLine())) {
    $id = $null
    try {
        $request = ConvertFrom-Json -InputObject $line
        $id = $request.id
        $text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($request.script))
        $response = @{ id = $id; ok = $true; result = (Get-Analysis $text) }
    } catch {
        $response = @{ id = $id; ok = $false; error = [string]$_.Exception.Message }
    }
    $writer.WriteLine((ConvertTo-Json -Compress -Depth 64 -InputObject $response))
    $writer.Flush()
}
