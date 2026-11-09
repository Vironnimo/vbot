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
$script:FixedVariables = @{}
foreach ($variable in Get-Variable) {
    if ($variable.Options -band ([System.Management.Automation.ScopedItemOptions]::ReadOnly -bor [System.Management.Automation.ScopedItemOptions]::Constant)) {
        $script:FixedVariables[$variable.Name.ToLowerInvariant()] = $true
    }
}

function New-List { return , [System.Collections.Generic.List[object]]::new() }

function Get-VariableName($VariableAst) {
    $name = $VariableAst.VariablePath.UserPath.ToLowerInvariant()
    foreach ($scope in 'global:', 'script:', 'local:', 'private:', 'using:') {
        if ($name.StartsWith($scope)) { return $name.Substring($scope.Length) }
    }
    return $name
}

function Get-ReferenceName($VariableAst) {
    # A scoped reference need not address a local assignment of the same name.
    return $VariableAst.VariablePath.UserPath.ToLowerInvariant()
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
                    $word.parts.Add(@{ k = 'v'; v = (Get-ReferenceName $nested) })
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
            else { $word.parts.Add(@{ k = 'v'; v = (Get-ReferenceName $Ast) }) }
            $word.target = Get-LoopTarget $Ast
        }
        'MemberExpressionAst' {
            $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text })
            if ($Ast.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
                $Ast.Member.Value -in 'FullName', 'Path') {
                $target = Get-LoopTarget $Ast.Expression
                # A literal string list names paths itself; its elements have
                # no FullName/Path property to use as a file target.
                if ($target -and $target.kind -ne 'paths') { $word.target = $target }
            }
        }
        'ArrayLiteralAst' {
            $word.items = (New-List)
            foreach ($element in $Ast.Elements) { $word.items.Add((ConvertTo-Word $element)) }
        }
        'ScriptBlockExpressionAst' { $word.block = $true; $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }) }
        'ParenExpressionAst' {
            # ('x') and ('a', 'b') are their value; any other pipeline is computed.
            $inner = Get-SoleExpression $Ast.Pipeline
            if ($null -ne $inner) {
                $converted = ConvertTo-Word $inner
                $converted.source = $Ast.Extent.Text
                # A native program gets ('x*') unquoted: pwsh expands it outside Windows.
                $converted.Remove('quoted')
                return $converted
            }
            $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text })
        }
        'CommandParameterAst' {
            $word.parameter = $Ast.ParameterName
            if ($null -ne $Ast.Argument) { $word.argument = (ConvertTo-Word $Ast.Argument) }
        }
        default { $word.parts.Add(@{ k = 'd'; v = $Ast.Extent.Text }) }
    }
    return $word
}

function Get-SoleExpression($Pipeline) {
    # The expression a pipeline of one expression statement without redirection holds.
    if ($Pipeline -is [System.Management.Automation.Language.PipelineAst] -and $Pipeline.PipelineElements.Count -eq 1) {
        $element = $Pipeline.PipelineElements[0]
        if ($element -is [System.Management.Automation.Language.CommandExpressionAst] -and $element.Redirections.Count -eq 0) {
            return $element.Expression
        }
    }
    return $null
}

function Test-ValueScope($Node) {
    # Immutable outer values survive branches and loops, but a function can
    # be called before its definition (and before a preceding assignment).
    for ($parent = $Node.Parent; $null -ne $parent; $parent = $parent.Parent) {
        if ($parent -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
            $parent -is [System.Management.Automation.Language.TrapStatementAst]) { return $false }
    }
    return $true
}

function Test-TextExpression($Node) {
    if ($Node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
        $Node -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { return $true }
    if ($Node -is [System.Management.Automation.Language.ParenExpressionAst]) {
        $inner = Get-SoleExpression $Node.Pipeline
        return $null -ne $inner -and (Test-TextExpression $inner)
    }
    if ($Node -is [System.Management.Automation.Language.ArrayLiteralAst]) {
        foreach ($element in $Node.Elements) { if (-not (Test-TextExpression $element)) { return $false } }
        return $true
    }
    if ($Node -is [System.Management.Automation.Language.BinaryExpressionAst]) {
        return [string]$Node.Operator -in 'Isplit', 'Csplit', 'Ireplace', 'Creplace', 'Join'
    }
    if ($Node -is [System.Management.Automation.Language.UnaryExpressionAst]) {
        return [string]$Node.TokenKind -in 'Split', 'Isplit', 'Csplit', 'Join'
    }
    if ($Node -is [System.Management.Automation.Language.ConvertExpressionAst]) {
        return $Node.Type.TypeName.FullName -in 'string', 'string[]', 'System.String', 'System.String[]'
    }
    if ($Node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $Node.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        if ($Node.Static -and $Node.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
            return (Get-TypeName $Node.Expression.TypeName) -in 'System.IO.File', 'IO.File' -and
                $Node.Member.Value -in 'ReadAllText', 'ReadAllLines'
        }
        return $Node.Member.Value -eq 'Replace' -and (Test-TextExpression $Node.Expression)
    }
    return $false
}

function Get-TextVariable($Node) {
    while ($Node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        -not $Node.Static -and $Node.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $Node.Member.Value -eq 'Replace') { $Node = $Node.Expression }
    if ($Node -is [System.Management.Automation.Language.VariableExpressionAst]) { return Get-ReferenceName $Node }
    return $null
}

function Get-ScalarWord($Node) {
    if ($Node -is [System.Management.Automation.Language.ParenExpressionAst]) {
        $Node = Get-SoleExpression $Node.Pipeline
    }
    if ($Node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
        $Node -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -or
        ($Node -is [System.Management.Automation.Language.VariableExpressionAst] -and -not $Node.Splatted)) {
        return ConvertTo-Word $Node
    }
    return $null
}

function Get-TypeName($TypeName) {
    $type = $null
    try { $type = $TypeName.GetReflectionType() } catch { }
    if ($type) { return $type.FullName }
    return $TypeName.FullName
}

function Get-Feed($Ast) {
    # Only the processing/filter block receives the pipeline item as $_.
    # Catch/trap handlers and nested/deferred blocks can give it another value.
    $node = $Ast.Parent
    while ($null -ne $node -and $node -isnot [System.Management.Automation.Language.ScriptBlockExpressionAst]) {
        if ($node -is [System.Management.Automation.Language.CatchClauseAst] -or
            $node -is [System.Management.Automation.Language.TrapStatementAst] -or
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $null }
        $node = $node.Parent
    }
    if ($null -eq $node) { return $null }
    $command = $node.Parent
    if ($command -is [System.Management.Automation.Language.CommandParameterAst]) {
        if ($command.ParameterName -notin 'Process', 'FilterScript', 'Parallel') { return $null }
        $command = $command.Parent
    }
    if ($command -isnot [System.Management.Automation.Language.CommandAst]) { return $null }
    if ([object]::ReferenceEquals($command.CommandElements[0], $node) -and
        [string]$command.InvocationOperator -in 'Ampersand', 'Dot') {
        $process = $node.ScriptBlock.ProcessBlock
        if ($process -and $Ast.Extent.StartOffset -ge $process.Extent.StartOffset -and
            $Ast.Extent.EndOffset -le $process.Extent.EndOffset) { return Get-Pipeline $command }
        return $null
    }
    $called = $command.GetCommandName()
    $info = $null
    if ($called) { [void]$script:Commands.TryGetValue($called, [ref]$info) }
    if ($info -and [string]$info.CommandType -eq 'Alias') { $info = $info.ResolvedCommand }
    if ($info -and $info.Name -in 'ForEach-Object', 'Where-Object') {
        try {
            $binding = [System.Management.Automation.Language.StaticParameterBinder]::BindCommand($command, $true)
            foreach ($parameter in 'Begin', 'End', 'RemainingScripts') {
                $bound = $binding.BoundParameters[$parameter]
                if ($bound -and $bound.Value -and
                    $node.Extent.StartOffset -ge $bound.Value.Extent.StartOffset -and
                    $node.Extent.EndOffset -le $bound.Value.Extent.EndOffset) { return $null }
            }
        } catch { return $null }
        return Get-Pipeline $command
    }
    return $null
}

function Get-LoopTarget($Expression) {
    if ($Expression -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $null }
    $name = Get-ReferenceName $Expression
    if ($name -in '_', 'psitem') {
        if ($script:AssignmentCounts['_'] -or $script:AssignmentCounts['psitem']) { return $null }
        $feed = Get-Feed $Expression
        if ($null -ne $feed) { return @{ kind = 'item'; feed = $feed; variable = $name } }
    }
    if ($script:AssignmentCounts[$name] -ne 1) { return $null }
    for ($parent = $Expression.Parent; $null -ne $parent; $parent = $parent.Parent) {
        if ($parent -is [System.Management.Automation.Language.FunctionDefinitionAst]) { break }
        if ($parent -isnot [System.Management.Automation.Language.ForEachStatementAst] -or
            (Get-ReferenceName $parent.Variable) -ne $name -or
            $Expression.Extent.StartOffset -lt $parent.Body.Extent.StartOffset) { continue }
        $condition = $parent.Condition
        $value = Get-SoleExpression $condition
        while ($value -is [System.Management.Automation.Language.ParenExpressionAst] -or
            $value -is [System.Management.Automation.Language.ArrayExpressionAst]) {
            if ($value -is [System.Management.Automation.Language.ParenExpressionAst]) { $condition = $value.Pipeline }
            elseif ($value.SubExpression.Statements.Count -eq 1) { $condition = $value.SubExpression.Statements[0] }
            else { return $null }
            $value = Get-SoleExpression $condition
        }
        if ($null -ne $value) {
            # An arbitrary variable's output is not a bounded file list.
            if ($value -is [System.Management.Automation.Language.ArrayLiteralAst] -or
                $value -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                return @{ kind = 'paths'; arguments = @((ConvertTo-Word $value)); variable = $name }
            }
            return $null
        }
        if ($condition -is [System.Management.Automation.Language.PipelineAst]) {
            if ($condition.PipelineElements.Count -gt 1) {
                return @{ kind = 'item'; variable = $name; feed = @{
                    id = $condition.Extent.StartOffset; index = $condition.PipelineElements.Count
                } }
            }
            $last = $condition.PipelineElements[-1]
            return @{ kind = 'command'; offset = $last.Extent.StartOffset; variable = $name }
        }
    }
    return $null
}

function Get-CallTarget($Expression) {
    # What a method is called on: a type, a new object, a command's output, the pipeline item.
    while ($Expression -is [System.Management.Automation.Language.ParenExpressionAst] -or $Expression -is [System.Management.Automation.Language.SubExpressionAst]) {
        if ($Expression -is [System.Management.Automation.Language.ParenExpressionAst]) { $pipeline = $Expression.Pipeline }
        elseif ($Expression.SubExpression.Statements.Count -eq 1) { $pipeline = $Expression.SubExpression.Statements[0] }
        else { return @{ kind = 'other' } }
        $inner = Get-SoleExpression $pipeline
        if ($null -ne $inner) { $Expression = $inner; continue }
        if ($pipeline -is [System.Management.Automation.Language.PipelineAst] -and $pipeline.PipelineElements.Count -ge 1) {
            $last = $pipeline.PipelineElements[$pipeline.PipelineElements.Count - 1]
            if ($last -is [System.Management.Automation.Language.CommandAst]) {
                return @{ kind = 'command'; offset = $last.Extent.StartOffset }
            }
        }
        return @{ kind = 'other' }
    }
    if ($Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
        return @{ kind = 'type'; type = (Get-TypeName $Expression.TypeName) }
    }
    if ($Expression -is [System.Management.Automation.Language.ConvertExpressionAst]) {
        $arguments = New-List
        $arguments.Add((ConvertTo-Word $Expression.Child))
        return @{ kind = 'new'; type = (Get-TypeName $Expression.Type.TypeName); arguments = $arguments }
    }
    if ($Expression -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $Expression.Static -and
        $Expression.Expression -is [System.Management.Automation.Language.TypeExpressionAst] -and
        $Expression.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and $Expression.Member.Value -eq 'new') {
        $arguments = New-List
        foreach ($argument in @($Expression.Arguments)) { if ($null -ne $argument) { $arguments.Add((ConvertTo-Word $argument)) } }
        return @{ kind = 'new'; type = (Get-TypeName $Expression.Expression.TypeName); arguments = $arguments }
    }
    if ($Expression -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
        $Expression -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
        return @{ kind = 'text' }
    }
    if (Test-TextExpression $Expression) { return @{ kind = 'text' } }
    if ($Expression -is [System.Management.Automation.Language.MemberExpressionAst] -and
        $Expression -isnot [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $Expression.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $Expression.Member.Value -in 'FullName', 'Name', 'BaseName', 'DirectoryName', 'Extension', 'Path', 'Line', 'Filename') {
        return @{ kind = 'property'; receiver = (Get-CallTarget $Expression.Expression) }
    }
    if ($Expression -is [System.Management.Automation.Language.VariableExpressionAst]) {
        $target = Get-LoopTarget $Expression
        if ($null -ne $target) { return $target }
        return @{ kind = 'variable'; variable = (Get-ReferenceName $Expression) }
    }
    $variable = Get-TextVariable $Expression
    if ($null -ne $variable) {
        return @{ kind = 'variable'; variable = $variable }
    }
    return @{ kind = 'other' }
}

function Get-Call($Node) {
    $arguments = New-List
    foreach ($argument in @($Node.Arguments)) { if ($null -ne $argument) { $arguments.Add((ConvertTo-Word $argument)) } }
    $member = $null
    if ($Node.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $member = $Node.Member.Value }
    return @{
        offset = $Node.Extent.EndOffset; source = $Node.Extent.Text; member = $member
        static = [bool]$Node.Static; target = (Get-CallTarget $Node.Expression); arguments = $arguments
    }
}

function Test-ExpressionString($Node) {
    # A string an expression names: not a command word, parameter value, redirection target,
    # member name or hashtable key, which have their own meaning.
    $parent = $Node.Parent
    while ($parent -is [System.Management.Automation.Language.ArrayLiteralAst]) { $parent = $parent.Parent }
    for ($ancestor = $parent; $null -ne $ancestor; $ancestor = $ancestor.Parent) {
        if ($ancestor -is [System.Management.Automation.Language.AssignmentStatementAst] -or
            $ancestor -is [System.Management.Automation.Language.ScriptBlockAst] -or
            $ancestor -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -or
            $ancestor -is [System.Management.Automation.Language.CommandAst] -or
            $ancestor -is [System.Management.Automation.Language.ConvertExpressionAst]) { break }
        # An outer text operation consumes a call's result, not its arguments.
        # Keep the path-like arguments of nested calls under their own checks.
        if ($ancestor -is [System.Management.Automation.Language.BinaryExpressionAst] -and
            [string]$ancestor.Operator -match '^(I|C)?(Split|Replace|Match|NotMatch|Like|NotLike)$') { return $false }
        if ($ancestor -is [System.Management.Automation.Language.UnaryExpressionAst] -and
            [string]$ancestor.TokenKind -match '^(I|C)?Split$') { return $false }
    }
    # Method arguments have their own semantics; file APIs are checked by
    # member_call. String replacement text is never an extra file access.
    if ($parent -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $parent.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $parent.Member.Value -eq 'Replace') { return $false }
    if ($parent -is [System.Management.Automation.Language.CommandAst] -or
        $parent -is [System.Management.Automation.Language.CommandParameterAst] -or
        $parent -is [System.Management.Automation.Language.FileRedirectionAst] -or
        $parent -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { return $false }
    if ($parent -is [System.Management.Automation.Language.MemberExpressionAst] -and [object]::ReferenceEquals($parent.Member, $Node)) { return $false }
    if ($parent -is [System.Management.Automation.Language.HashtableAst]) {
        foreach ($pair in $parent.KeyValuePairs) { if ([object]::ReferenceEquals($pair.Item1, $Node)) { return $false } }
    }
    return $true
}

function Test-MethodArgumentString($Node) {
    for ($ancestor = $Node.Parent; $null -ne $ancestor; $ancestor = $ancestor.Parent) {
        if ($ancestor -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
            foreach ($argument in @($ancestor.Arguments)) {
                if ($null -ne $argument -and
                    $argument.Extent.StartOffset -le $Node.Extent.StartOffset -and
                    $argument.Extent.EndOffset -ge $Node.Extent.EndOffset) { return $true }
            }
            return $false
        }
        if ($ancestor -is [System.Management.Automation.Language.CommandAst] -or
            $ancestor -is [System.Management.Automation.Language.AssignmentStatementAst] -or
            $ancestor -is [System.Management.Automation.Language.ScriptBlockAst]) { return $false }
    }
    return $false
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
    $result = @{ errors = (New-List); incomplete = $false; commands = (New-List); assigned = (New-List); functions = (New-List); environment_changed = $false; calls = (New-List); strings = (New-List); items = (New-List); assignments = (New-List) }
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
                ($node -is [System.Management.Automation.Language.ConvertExpressionAst] -and $node.Type.TypeName.FullName -match '^(ref|System.Management.Automation.PSReference)(\[\])?$') -or
                $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -or
                ($node -is [System.Management.Automation.Language.UnaryExpressionAst] -and
                    ([string]$node.TokenKind -match 'PlusPlus|MinusMinus|PostfixPlusPlus|PostfixMinusMinus'))
            }, $true)) {
        $targets = @()
        if ($node -is [System.Management.Automation.Language.AssignmentStatementAst]) {
            $left = $node.Left
            while ($left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $left = $left.Child }
            # FileSystem metadata setters change the item, not the receiver's
            # identity or path. Keep its bounds for the set_<Name> file access.
            # Other property writes remain invalidating: MatchInfo.Path, for
            # example, can redirect a later Get-Content $_.Path elsewhere.
            if ($left -is [System.Management.Automation.Language.MemberExpressionAst] -and
                $left -isnot [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
                -not $left.Static -and
                $left.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
                $left.Member.Value -in 'IsReadOnly', 'Attributes', 'CreationTime', 'CreationTimeUtc',
                    'LastWriteTime', 'LastWriteTimeUtc', 'LastAccessTime', 'LastAccessTimeUtc', 'UnixFileMode') {
                continue
            }
            $targets = @($node.Left)
        }
        elseif ($node -is [System.Management.Automation.Language.ForEachStatementAst]) { $targets = @($node.Variable) }
        elseif ($node -is [System.Management.Automation.Language.ParameterAst]) { $targets = @($node.Name) }
        elseif ($node -is [System.Management.Automation.Language.UnaryExpressionAst]) { $targets = @($node.Child) }
        elseif ($node -is [System.Management.Automation.Language.ConvertExpressionAst]) { $targets = @($node.Child) }
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

    # Only an untyped, unique assignment executed directly by this script can
    # establish a value. Branches, loop variables, parameters and refs stay unknown.
    $assignmentCounts = @{}
    foreach ($name in $result.assigned) { $assignmentCounts[$name] = 1 + $assignmentCounts[$name] }
    $script:AssignmentCounts = $assignmentCounts
    $directCounts = @{}
    foreach ($node in $ast.EndBlock.Statements) {
        if ($node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            [string]$node.Operator -eq 'Equals' -and
            $node.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $name = Get-ReferenceName $node.Left
            $directCounts[$name] = 1 + $directCounts[$name]
        }
    }
    foreach ($node in $ast.EndBlock.Statements) {
        if ($node -isnot [System.Management.Automation.Language.AssignmentStatementAst] -or
            [string]$node.Operator -ne 'Equals' -or
            $node.Left -isnot [System.Management.Automation.Language.VariableExpressionAst]) { continue }
        $name = $node.Left.VariablePath.UserPath.ToLowerInvariant()
        if ($name -notmatch '^[a-z_][a-z0-9_]*$' -or $assignmentCounts[$name] -ne $directCounts[$name] -or
            $script:FixedVariables.ContainsKey($name) -or
            $name -in 'pwd', '_', 'psitem', 'lastexitcode', 'matches') { continue }
        $entry = @{ name = $name; offset = $node.Extent.EndOffset; text = $false }
        if ($node.Right -is [System.Management.Automation.Language.CommandExpressionAst] -and $node.Right.Redirections.Count -eq 0) {
            if ($assignmentCounts[$name] -eq 1) { $entry.value = Get-ScalarWord $node.Right.Expression }
            $entry.text = Test-TextExpression $node.Right.Expression
            $entry.text_from = Get-TextVariable $node.Right.Expression
        } elseif ($node.Right -is [System.Management.Automation.Language.PipelineAst] -and
            $node.Right.PipelineElements.Count -eq 1 -and
            $node.Right.PipelineElements[0] -is [System.Management.Automation.Language.CommandAst]) {
            $called = $node.Right.PipelineElements[0].GetCommandName()
            $info = $null
            if ($called -and -not $functions.Contains($called)) { [void]$script:Commands.TryGetValue($called, [ref]$info) }
            if ($info -and [string]$info.CommandType -eq 'Alias') { $info = $info.ResolvedCommand }
            $entry.text = $info -and $info.Name -in 'Get-Content', 'Out-String' -and
                -not ($node.Right.Extent.Text -match '-AsByteStream\b')
            if ($info -and $info.Name -eq 'Join-Path' -and $assignmentCounts[$name] -eq 1) {
                $binding = Get-Binding $node.Right.PipelineElements[0]
                if ($binding.errors.Count -eq 0 -and -not $binding.bound.ContainsKey('Resolve') -and
                    $binding.bound.ContainsKey('Path') -and $binding.bound.ContainsKey('ChildPath')) {
                    $parts = New-List
                    $parts.Add($binding.bound.Path)
                    $parts.Add($binding.bound.ChildPath)
                    if ($binding.bound.ContainsKey('AdditionalChildPath')) { $parts.Add($binding.bound.AdditionalChildPath) }
                    $entry.value = @{ source = $node.Right.Extent.Text; join = $parts }
                }
            }
        }
        $result.assignments.Add($entry)
    }

    foreach ($node in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandBaseAst] }, $true)) {
        $entry = @{ offset = $node.Extent.StartOffset; source = $node.Extent.Text; redirects = (Get-Redirections $node); pipeline = (Get-Pipeline $node); top_level = (Test-ValueScope $node) }
        if ($node -isnot [System.Management.Automation.Language.CommandAst]) {
            $entry.expression = $true
            $entry.output_text = Test-TextExpression $node.Expression
            if ($node.Expression -is [System.Management.Automation.Language.VariableExpressionAst]) {
                $entry.output_variable = Get-ReferenceName $node.Expression
            }
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
    # .NET method calls and the strings expressions name, for paths and URLs outside commands.
    foreach ($node in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
        $call = Get-Call $node
        $call.top_level = Test-ValueScope $node
        $result.calls.Add($call)
    }
    # A property assignment ((gi x).IsReadOnly = $true) is a call of set_<Name>.
    foreach ($node in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        $left = $node.Left
        while ($left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $left = $left.Child }
        if ($left -isnot [System.Management.Automation.Language.MemberExpressionAst] -or
            $left -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -or
            $left.Member -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { continue }
        $result.calls.Add(@{
            offset = $node.Extent.EndOffset; source = $node.Extent.Text; member = 'set_' + $left.Member.Value
            static = [bool]$left.Static; target = (Get-CallTarget $left.Expression); arguments = (New-List)
        })
    }
    foreach ($node in $ast.FindAll({ param($node)
                $node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
                $node -is [System.Management.Automation.Language.ExpandableStringExpressionAst]
            }, $true)) {
        if (Test-ExpressionString $node) {
            $string = @{ offset = $node.Extent.StartOffset; word = (ConvertTo-Word $node) }
            if (Test-MethodArgumentString $node) { $string.argument = $true }
            $result.strings.Add($string)
        }
    }
    # ${C:\x} and $c:x name an item of a provider drive: its content is read, or
    # written when the variable is assigned to (=, +=, ++, foreach).
    foreach ($node in $ast.FindAll({ param($node)
                $node -is [System.Management.Automation.Language.VariableExpressionAst] -and $node.VariablePath.IsDriveQualified
            }, $true)) {
        if ($node.VariablePath.DriveName -in 'env', 'variable', 'function', 'alias') { continue }
        $written = $false
        for ($parent = $node.Parent; $null -ne $parent; $parent = $parent.Parent) {
            if ($parent -is [System.Management.Automation.Language.AssignmentStatementAst]) {
                $left = $parent.Left.Extent
                $written = $node.Extent.StartOffset -ge $left.StartOffset -and $node.Extent.EndOffset -le $left.EndOffset
                break
            }
            if ($parent -is [System.Management.Automation.Language.ForEachStatementAst]) {
                $written = [object]::ReferenceEquals($parent.Variable, $node)
                break
            }
            if ($parent -is [System.Management.Automation.Language.UnaryExpressionAst] -and
                ([string]$parent.TokenKind -match 'PlusPlus|MinusMinus')) {
                $written = $true
                break
            }
            if ($parent -is [System.Management.Automation.Language.StatementAst]) { break }
        }
        $result.items.Add(@{ offset = $node.Extent.StartOffset; source = $node.Extent.Text; path = $node.VariablePath.UserPath; write = $written })
    }
    return $result
}

# Warm up: the first analysis compiles the code paths every later one takes.
$warmUp = 'Get-ChildItem -Path x | Remove-Item -Recurse; tool "$env:HOME" > $null; [IO.File]::Delete((''y''))'
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
