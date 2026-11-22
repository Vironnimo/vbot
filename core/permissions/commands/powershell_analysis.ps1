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

function Get-FullNameTargets($Node) {
    if ($Node -isnot [System.Management.Automation.Language.MemberExpressionAst] -or
        $Node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -or $Node.Static -or
        $Node.Member -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
        $Node.Member.Value -ne 'FullName' -or $Node.Expression -isnot [System.Management.Automation.Language.ParenExpressionAst]) { return $null }
    $pipeline = $Node.Expression.Pipeline
    if ($pipeline -isnot [System.Management.Automation.Language.PipelineAst] -or $pipeline.PipelineElements.Count -ne 1) { return $null }
    $cmd = $pipeline.PipelineElements[0]
    if ($cmd -isnot [System.Management.Automation.Language.CommandAst] -or $cmd.Redirections.Count) { return $null }
    $name = $cmd.GetCommandName()
    if ($name -notin 'Get-Item', 'Get-ChildItem' -or $functions.Contains($name)) { return $null }
    $binding = [System.Management.Automation.Language.StaticParameterBinder]::BindCommand($cmd, $true)
    if ($binding.BindingExceptions.Count -or @($binding.BoundParameters.Keys | Where-Object { $_ -notin 'Path', 'LiteralPath', 'Filter', 'File' }).Count) { return $null }
    $paths = $binding.BoundParameters['Path']
    if (-not $paths) { $paths = $binding.BoundParameters['LiteralPath'] }
    if (-not $paths) { return $null }
    $values = $paths.Value
    if ($values -is [System.Management.Automation.Language.ArrayLiteralAst]) { $values = $values.Elements }
    $arguments = New-List
    foreach ($value in $values) {
        if ($value -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
            $value.Value.IndexOfAny([char[]]'*?[]') -ge 0) { return $null }
        $arguments.Add((ConvertTo-Word $value))
    }
    return @{ kind = 'projected'; arguments = $arguments; children = ($name -eq 'Get-ChildItem') }
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
                elseif ($Ast.Member.Value -eq 'FullName') { $word.target = Get-FullNameTargets $Ast }
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
            $pipeline = $Ast.Pipeline
            if ($pipeline -is [System.Management.Automation.Language.PipelineAst] -and $pipeline.PipelineElements.Count -eq 1) {
                $cmd = $pipeline.PipelineElements[0]
                if ($cmd -is [System.Management.Automation.Language.CommandAst] -and -not $cmd.Redirections.Count) {
                    $name = $cmd.GetCommandName()
                    if ($name -eq 'Get-Location' -and $cmd.CommandElements.Count -eq 1 -and -not $functions.Contains($name)) {
                        $word.parts.Add(@{ k = 'v'; v = 'pwd' })
                        $word.current_location = $true
                        return $word
                    }
                    if ($name -eq 'Join-Path' -and -not $functions.Contains($name)) {
                        $join = Get-LiteralJoin $cmd
                        if ($join) { return $join }
                    }
                }
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

function Get-VariableDriveTest($Node) {
    if ($Node -is [System.Management.Automation.Language.ParenExpressionAst]) {
        $Node = Get-SoleExpression $Node.Pipeline
    }
    if ($Node -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -and
        $Node.NestedExpressions.Count -eq 1 -and
        $Node.Value -ceq ('Variable:' + $Node.NestedExpressions[0].Extent.Text)) {
        $ref = $Node.NestedExpressions[0]
    } elseif ($Node -is [System.Management.Automation.Language.BinaryExpressionAst] -and
        [string]$Node.Operator -eq 'Plus' -and $Node.Left -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $Node.Left.Value -eq 'Variable:') { $ref = $Node.Right }
    else { return $null }
    if ($ref -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $null }
    $target = Get-LoopTarget $ref
    if (-not $target) { return $null }
    if ($target.kind -eq 'item') {
        $pipeline = $ref.Parent
        while ($pipeline -and -not ($pipeline -is [System.Management.Automation.Language.PipelineAst] -and
            $pipeline.Extent.StartOffset -eq $target.feed.id)) { $pipeline = $pipeline.Parent }
        if (-not $pipeline -or $target.feed.index -ne 1 -or
            $pipeline.PipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst] -or
            $pipeline.PipelineElements[0].Redirections.Count) { return $null }
        $source = $pipeline.PipelineElements[0].Expression
        $list = Get-LiteralList $source
        if (-not $list) { return $null }
        $target = @{ kind = 'paths'; variable = $target.variable; list_variable = (Get-ReferenceName $source); arguments = @((ConvertTo-Word $list)) }
    }
    if ($target.kind -ne 'paths') { return $null }
    foreach ($word in $target.arguments) {
        foreach ($item in @($word.items)) {
            if ($null -eq $item -or $item.parts.Count -ne 1 -or $item.parts[0].k -ne 's' -or
                $item.parts[0].v -notmatch '^[a-zA-Z_][a-zA-Z0-9_]*$') { return $null }
        }
    }
    return $target
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
    # Only the expression of a calculated Select-Object property receives the input item.
    if ($command -is [System.Management.Automation.Language.CommandExpressionAst] -and
        $command.Redirections.Count -eq 0 -and
        $command.Parent -is [System.Management.Automation.Language.PipelineAst] -and
        $command.Parent.PipelineElements.Count -eq 1 -and
        $command.Parent.Parent -is [System.Management.Automation.Language.HashtableAst]) {
        $table = $command.Parent.Parent
        $pair = @($table.KeyValuePairs | Where-Object {
            $_.Item1 -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
            $_.Item1.Value -in 'Expression', 'Expr', 'E' -and
            [object]::ReferenceEquals($_.Item2, $command.Parent)
        })
        $owner = $table.Parent
        if ($owner -is [System.Management.Automation.Language.ArrayLiteralAst]) { $owner = $owner.Parent }
        if ($pair.Count -eq 1 -and $owner -is [System.Management.Automation.Language.CommandAst] -and
            $owner.GetCommandName() -eq 'Select-Object' -and -not $functions.Contains('Select-Object')) {
            $binding = [System.Management.Automation.Language.StaticParameterBinder]::BindCommand($owner, $true)
            $property = $binding.BoundParameters['Property']
            if ($property -and -not $binding.BoundParameters.ContainsKey('InputObject') -and
                $table.Extent.StartOffset -ge $property.Value.Extent.StartOffset -and
                $table.Extent.EndOffset -le $property.Value.Extent.EndOffset) { return Get-Pipeline $owner }
        }
        return $null
    }
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
            if ($binding.BoundParameters.ContainsKey('InputObject')) { return $null }
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

function Get-LiteralList($Reference) {
    if ($Reference -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $null }
    $name = Get-ReferenceName $Reference
    $definition = $literalLists[$name]
    if (-not $definition -or $definition.Extent.EndOffset -ge $Reference.Extent.StartOffset -or
        -not (Test-ValueScope $Reference)) { return $null }
    # An array can be changed through another reference. Accept an immediately
    # following read-only enumeration, with no other reference to the array.
    if ($listUses[$name] -ne 2) { return $null }
    $statements = $definition.Parent.Statements
    $index = $statements.IndexOf($definition) + 1
    if ($index -ge $statements.Count) { return $null }
    $next = $statements[$index]
    if ($Reference.Extent.StartOffset -lt $next.Extent.StartOffset -or
        $Reference.Extent.EndOffset -gt $next.Extent.EndOffset) { return $null }
    $unknown = $next.Find({ param($n)
        $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -or
        ($n -is [System.Management.Automation.Language.CommandAst] -and
            $n.GetCommandName() -notin 'Get-Content', 'Measure-Object', 'Test-Path', 'ForEach-Object', 'Select-String', 'rg')
    }, $true)
    if (-not $unknown) { return $definition.Right.Expression }
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
        if ($value -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $list = Get-LiteralList $value
            if ($list) {
                return @{ kind = 'paths'; arguments = @((ConvertTo-Word $list)); variable = $name; list_variable = (Get-ReferenceName $value) }
            }
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
    # A separator appended to replacement text is not a separate filesystem read.
    if ($parent -is [System.Management.Automation.Language.BinaryExpressionAst] -and
        [string]$parent.Operator -eq 'Plus' -and $Node.Value -in '/', '\' -and
        $parent.Parent -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and
        $parent.Parent.Member -is [System.Management.Automation.Language.StringConstantExpressionAst] -and
        $parent.Parent.Member.Value -eq 'Replace') { return $false }
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
            $word = ConvertTo-Word $value
            if ($CommandAst.GetCommandName() -eq 'Test-Path' -and $entry.Key -in 'Path', 'LiteralPath') {
                $word.variable_test = Get-VariableDriveTest $value
            }
            $bound[$entry.Key] = $word
        }
    }
    foreach ($key in $binding.BindingExceptions.Keys) { $errors.Add([string]$key) }
    return @{ bound = $bound; unbound = $unbound; errors = $errors }
}

function Get-LiteralJoin($Command) {
    $binding = Get-Binding $Command
    if ($binding.errors.Count -or $binding.unbound.Count -or
        @($binding.bound.Keys | Where-Object { $_ -notin 'Path', 'ChildPath', 'AdditionalChildPath' }).Count -or
        -not $binding.bound.ContainsKey('Path') -or -not $binding.bound.ContainsKey('ChildPath')) { return $null }
    $parts = New-List
    $parts.Add($binding.bound.Path)
    $parts.Add($binding.bound.ChildPath)
    if ($binding.bound.ContainsKey('AdditionalChildPath')) { $parts.Add($binding.bound.AdditionalChildPath) }
    return @{ source = $Command.Extent.Text; parts = @(@{ k = 'd'; v = $Command.Extent.Text }); join = $parts }
}

function Test-LiteralCliSyntax($Ast) {
    # Check the original syntax tree: the ordinary command plan deliberately
    # omits standalone expressions and resolves some variable assignments.
    if ($null -ne $Ast.ScriptRequirements) { return $false }
    $other = $Ast.Find({ param($node)
        switch ($node.GetType().Name) {
            'ScriptBlockAst' { return ($null -ne $node.Parent) }
            'NamedBlockAst' { return (-not $node.Unnamed) }
            'PipelineAst' { return [bool]$node.Background }
            'PipelineChainAst' { return [bool]$node.Background }
            'CommandAst' { return ($node.CommandElements[0].Extent.Text -ine 'vbot' -or [string]$node.InvocationOperator -ne 'Unknown') }
            'CommandParameterAst' { return $false }
            'ConstantExpressionAst' { return $false }
            'StringConstantExpressionAst' {
                if ([string]$node.StringConstantType -ne 'BareWord') { return $false }
                # Stop-parsing tails have native environment substitution on Windows.
                return ($node.Value -eq '--%' -or $node.Value -notmatch '^[\w./\\:=,@%+\-]+$')
            }
            'FileRedirectionAst' { return $false }
            'MergingRedirectionAst' { return $false }
            default { return $true }
        }
    }, $true)
    return ($null -eq $other)
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
    if ($result.errors.Count -gt 0) {
        $first = $parseErrors[0]
        $result.syntax_error = @{
            id = $first.ErrorId; line = $first.Extent.StartLineNumber; column = $first.Extent.StartColumnNumber
            backslash_quote = [bool]($tokens | Where-Object {
                $_.Kind -eq 'StringExpandable' -and $_.Text.Contains('\"')
            } | Select-Object -First 1)
        }
        return $result
    }
    if ($Text.IndexOf('vbot', [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
        $result.literal_cli_syntax = Test-LiteralCliSyntax $ast
    }

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
    $literalLists = @{}
    $directCounts = @{}
    foreach ($node in $ast.EndBlock.Statements) {
        if ($node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            [string]$node.Operator -eq 'Equals' -and
            $node.Left -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $name = Get-ReferenceName $node.Left
            $directCounts[$name] = 1 + $directCounts[$name]
            if ($assignmentCounts[$name] -eq 1 -and -not $script:FixedVariables.ContainsKey($name) -and
                $node.Right -is [System.Management.Automation.Language.CommandExpressionAst] -and
                $node.Right.Expression -is [System.Management.Automation.Language.ArrayLiteralAst] -and
                $node.Right.Redirections.Count -eq 0 -and
                @($node.Right.Expression.Elements | Where-Object {
                    $_ -isnot [System.Management.Automation.Language.StringConstantExpressionAst]
                }).Count -eq 0) { $literalLists[$name] = $node }
        }
    }
    $listUses = @{}
    if ($literalLists.Count) {
        foreach ($ref in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
            $name = Get-VariableName $ref
            if ($literalLists.ContainsKey($name)) { $listUses[$name] = 1 + $listUses[$name] }
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
                $entry.value = Get-LiteralJoin $node.Right.PipelineElements[0]
                $entry.initial = -not $ast.ParamBlock -and -not $ast.BeginBlock -and -not $ast.ProcessBlock -and
                    -not $ast.DynamicParamBlock -and $ast.EndBlock.Unnamed -and
                    [object]::ReferenceEquals($node, $ast.EndBlock.Statements[0]) -and
                    $entry.value -and $entry.value.join[0].current_location
            }
        }
        $result.assignments.Add($entry)
    }

    foreach ($node in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandBaseAst] }, $true)) {
        $entry = @{ offset = $node.Extent.StartOffset; source = $node.Extent.Text; redirects = (Get-Redirections $node); pipeline = (Get-Pipeline $node); top_level = (Test-ValueScope $node) }
        if ($node -isnot [System.Management.Automation.Language.CommandAst]) {
            $entry.expression = $true
            $entry.output_text = Test-TextExpression $node.Expression
            $projection = Get-FullNameTargets $node.Expression
            if ($projection) { $entry.projected = $projection }
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
