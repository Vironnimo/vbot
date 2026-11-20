using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Management.Automation;
using System.Management.Automation.Host;
using System.Management.Automation.Language;
using System.Management.Automation.Runspaces;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using Microsoft.PowerShell.Commands;
public sealed class VBotPowerShellHost : PSHost, IHostSupportsInteractiveSession {
    readonly PSHost inner;
    bool promptInputEnded;
    public int? ExitCode;
    public PSVariableIntrinsics Variables;
    public Runspace CommandRunspace;
    public IList Errors;
    public object[] Record;
    public object NativeExit;
    public VBotPowerShellHost(PSHost inner) { this.inner = inner; }
    public override string Name => inner.Name;
    public override Version Version => inner.Version;
    public override Guid InstanceId => inner.InstanceId;
    public override PSHostUserInterface UI => inner.UI;
    public override CultureInfo CurrentCulture => inner.CurrentCulture;
    public override CultureInfo CurrentUICulture => inner.CurrentUICulture;
    public override PSObject PrivateData => inner.PrivateData;
    public override void SetShouldExit(int code) {
        ExitCode = code;
        inner.SetShouldExit(code);
    }
    public override void EnterNestedPrompt() {
        // The request consumed stdin to EOF. ConsoleHost would evaluate one
        // prompt and return. Its own runspace is waiting for this one, so the
        // prompt must run here; delegating its input loop would deadlock.
        if (ExitCode.HasValue || promptInputEnded) return;
        if (UI.RawUI.CursorPosition.X != 0) UI.WriteLine();
        string prompt = null;
        try {
            using (var pipeline = CommandRunspace.CreateNestedPipeline("prompt", false)) {
                var output = pipeline.Invoke();
                if (output.Count != 0) prompt = output[0]?.BaseObject?.ToString();
            }
        }
        catch { } // ConsoleHost falls back when prompt evaluation fails.
        UI.Write(string.IsNullOrEmpty(prompt) ? "PS>" : prompt);
        promptInputEnded = true;
    }
    public override void ExitNestedPrompt() => inner.ExitNestedPrompt();
    public override void NotifyBeginApplication() {
        inner.NotifyBeginApplication();
        RecordCommand();
    }
    public override void NotifyEndApplication() {
        try { NativeExit = Variables.GetValue("global:LASTEXITCODE"); }
        catch { Record = null; NativeExit = null; }
        finally { inner.NotifyEndApplication(); }
    }
    public void RecordCommand() {
        try {
            var frames = CommandRunspace.Debugger.GetCallStack().ToArray();
            Record = frames.Length == 0 ? null : new object[] { frames, frames[0].Position,
                Variables.GetValue("global:LASTEXITCODE"), Errors.Count == 0 ? null : Errors[0] };
        }
        catch { Record = null; NativeExit = null; }
    }
    // Direct arguments bind before BeginProcessing. Keep PowerShell's own
    // exception and position, but let it escape the statement like a throw.
    // A script can still catch it. Observe only the command pipeline's invoke.
    public void WatchLocationBinding(bool enabled) {
        if (enabled) AppDomain.CurrentDomain.FirstChanceException += GuardLocationBinding;
        else AppDomain.CurrentDomain.FirstChanceException -= GuardLocationBinding;
    }
    void GuardLocationBinding(object sender, FirstChanceExceptionEventArgs args) {
        if (args.Exception is ParameterBindingException error &&
            error.CommandInvocation?.MyCommand is CmdletInfo cmd &&
            (cmd.ImplementingType == typeof(VBotSetLocation) ||
             cmd.ImplementingType == typeof(VBotPushLocation)) &&
            System.Management.Automation.Runspaces.Runspace.DefaultRunspace == CommandRunspace) {
            error.WasThrownFromThrowStatement = true;
        }
    }
    public EventHandler<CommandLookupEventArgs> LookupHook => OnCommandLookup;
    void OnCommandLookup(object sender, CommandLookupEventArgs lookup) {
        try {
            var command = lookup.Command;
            if (command is AliasInfo alias) command = alias.ResolvedCommand;
            if (command is ExternalScriptInfo) RecordCommand();
        }
        catch { Record = null; NativeExit = null; }
    }
    public bool IsRunspacePushed => ((IHostSupportsInteractiveSession)inner).IsRunspacePushed;
    public Runspace Runspace => ((IHostSupportsInteractiveSession)inner).Runspace;
    public void PushRunspace(Runspace runspace) =>
        ((IHostSupportsInteractiveSession)inner).PushRunspace(runspace);
    public void PopRunspace() => ((IHostSupportsInteractiveSession)inner).PopRunspace();
}
public static class VBotUnwritableVariables {
    public static ParseError[] Find(ScriptBlockAst root, PSVariableIntrinsics variables) {
        var errors = new List<ParseError>();
        foreach (var found in root.FindAll(ast => ast is VariableExpressionAst, true)) {
            var node = (VariableExpressionAst)found;
            var path = node.VariablePath;
            if (!path.IsVariable) continue;
            Ast target = node, parent = node.Parent;
            while ((parent is AttributedExpressionAst attributed && attributed.Child == target) ||
                parent is ArrayLiteralAst) {
                target = parent;
                parent = parent.Parent;
            }
            var written = false;
            if (parent is AssignmentStatementAst assignment) written = assignment.Left == target;
            else if (parent is UnaryExpressionAst unary) {
                var kind = unary.TokenKind;
                written = kind == TokenKind.PlusPlus || kind == TokenKind.MinusMinus ||
                    kind == TokenKind.PostfixPlusPlus || kind == TokenKind.PostfixMinusMinus;
            }
            else if (parent is ForEachStatementAst loop) written = loop.Variable == node;
            else if (parent is ParameterAst parameter) written = parameter.Name == node;
            if (!written) continue;
            var name = path.UserPath.Substring(path.UserPath.LastIndexOf(':') + 1);
            var variable = variables.Get(name);
            var fixedValue = ScopedItemOptions.ReadOnly | ScopedItemOptions.Constant;
            if (variable == null || (variable.Options & fixedValue) == 0) continue;
            var everywhere = (variable.Options & ScopedItemOptions.AllScope) != 0;
            if (!everywhere && !path.IsGlobal && !path.IsScript) {
                Ast scope = node.Parent;
                while (scope != null && !(scope is ScriptBlockAst) &&
                    !(scope is FunctionDefinitionAst)) {
                    scope = scope.Parent;
                }
                if (scope != root) continue;
            }
            var message = "Cannot overwrite variable " + variable.Name +
                " because it is read-only or constant.";
            errors.Add(new ParseError(node.Extent, "VariableNotWritable", message));
        }
        return errors.ToArray();
    }
}
public static class VBotUnassignedVariables {
    // Set by PowerShell only while code runs, or absent until first used.
    static readonly HashSet<string> Runtime = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
        "_", "PSItem", "args", "input", "this", "Matches", "LASTEXITCODE", "PSBoundParameters",
        "MyInvocation", "PSCmdlet", "PSScriptRoot", "PSCommandPath", "foreach", "switch", "Event",
        "EventArgs", "EventSubscriber", "Sender", "SourceArgs", "SourceEventArgs", "StackTrace",
        "OFS", "PSDebugContext", "PSSenderInfo", "PSModuleAutoLoadingPreference", "PSLogUserData",
        "null", "true", "false", "?", "^", "$"
    };
    // Commands that create variables whose names the text does not show.
    static readonly HashSet<string> Dynamic = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
        "Invoke-Expression", "iex", "Import-Module", "ipmo", "Set-Variable", "sv", "set",
        "New-Variable", "nv", "Import-LocalizedData"
    };
    static readonly HashSet<string> Short = new HashSet<string>(StringComparer.OrdinalIgnoreCase) {
        "ov", "ev", "wv", "iv", "pv", "sv"
    };
    static string Name(VariableExpressionAst node) {
        var path = node.VariablePath.UserPath;
        return path.Substring(path.LastIndexOf(':') + 1);
    }
    public static ParseError[] Find(ScriptBlockAst root, PSVariableIntrinsics variables) {
        var none = new ParseError[0];
        var text = root.Extent.Text;
        if (text.IndexOf("variable:", StringComparison.OrdinalIgnoreCase) >= 0 ||
            text.IndexOf("PSVariable", StringComparison.OrdinalIgnoreCase) >= 0 ||
            text.IndexOf("InvokeScript", StringComparison.OrdinalIgnoreCase) >= 0) return none;
        var assigned = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var reads = new List<VariableExpressionAst>();
        foreach (var found in root.FindAll(ast => true, true)) {
            if (found is UsingStatementAst) return none;
            if (found is CommandAst command) {
                if (command.InvocationOperator == TokenKind.Dot) return none;
                var called = command.GetCommandName();
                if (called != null &&
                    Dynamic.Contains(called.Substring(called.LastIndexOf('\\') + 1))) return none;
                var elements = command.CommandElements;
                for (var index = 1; index < elements.Count; index++) {
                    var option = elements[index] as CommandParameterAst;
                    if (option == null) continue;
                    if (!Short.Contains(option.ParameterName) && option.ParameterName.IndexOf(
                        "var", StringComparison.OrdinalIgnoreCase) < 0) continue;
                    var value = option.Argument ??
                        (index + 1 < elements.Count ? elements[index + 1] : null);
                    if (value is StringConstantExpressionAst named) {
                        assigned.Add(named.Value.TrimStart('+'));
                    }
                    else if (value != null && !(value is CommandParameterAst)) return none;
                }
                continue;
            }
            var node = found as VariableExpressionAst;
            if (node == null || !node.VariablePath.IsVariable) continue;
            Ast target = node, parent = node.Parent;
            if (parent is UsingExpressionAst) continue;
            if (parent is ConvertExpressionAst reference && reference.Type.TypeName.Name.Equals(
                "ref", StringComparison.OrdinalIgnoreCase)) {
                assigned.Add(Name(node));
                continue;
            }
            while ((parent is AttributedExpressionAst attributed && attributed.Child == target) ||
                parent is ArrayLiteralAst || parent is ParenExpressionAst) {
                target = parent;
                parent = parent.Parent;
            }
            var written = false;
            if (parent is AssignmentStatementAst assignment) written = assignment.Left == target;
            else if (parent is UnaryExpressionAst unary) {
                var kind = unary.TokenKind;
                written = kind == TokenKind.PlusPlus || kind == TokenKind.MinusMinus ||
                    kind == TokenKind.PostfixPlusPlus || kind == TokenKind.PostfixMinusMinus;
            }
            else if (parent is ForEachStatementAst loop) written = loop.Variable == node;
            else if (parent is ParameterAst parameter) written = parameter.Name == node;
            if (written) assigned.Add(Name(node));
            else reads.Add(node);
        }
        var errors = new List<ParseError>();
        var reported = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var node in reads) {
            var name = Name(node);
            if (assigned.Contains(name) || Runtime.Contains(name) || !reported.Add(name)) continue;
            if (variables.Get(name) != null) continue;
            errors.Add(new ParseError(node.Extent, "VariableIsUndefined",
                "The variable '$" + name + "' cannot be retrieved because it has not been set."));
        }
        return errors.ToArray();
    }
}
public static class VBotGuard {
    static string[] folders = Array.Empty<string>();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    static extern uint GetFullPathNameW(string path, uint size, StringBuilder full, IntPtr filePart);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, FileShare share,
        IntPtr security, FileMode mode, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder path,
        uint size, uint flags);
    static string Normal(string path) {
        try {
            if (path.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) {
                path = @"\\" + path.Substring(8);
            } else if (path.StartsWith(@"\\?\", StringComparison.Ordinal) &&
                path.Length >= 7 && path[5] == ':' && path[6] == '\\') {
                path = path.Substring(4);
            }
            // Lexical only. Path.GetFullPath may query the filesystem to expand a
            // short name, including a registered folder on an unrelated, offline share.
            uint size = GetFullPathNameW(path, 0, null, IntPtr.Zero);
            if (size == 0) return null;
            var buffer = new StringBuilder(checked((int)size));
            uint length = GetFullPathNameW(path, size, buffer, IntPtr.Zero);
            return length > 0 && length < size
                ? Path.TrimEndingDirectorySeparator(buffer.ToString()) : null;
        }
        catch { return null; }
    }
    // Resolve short names and intermediate junctions from the open directory itself.
    // Removing or moving the link as a leaf operates on the link; folder/* follows it.
    static string DirectoryPath(string path, bool followLeaf) {
        const uint directoryHandle = 0x02000000, openReparsePoint = 0x00200000;
        try {
            if (!Directory.Exists(path)) return null;
            using (var handle = CreateFileW(path, 0, FileShare.ReadWrite | FileShare.Delete,
                IntPtr.Zero, FileMode.Open, directoryHandle | (followLeaf ? 0 : openReparsePoint),
                IntPtr.Zero)) {
                if (handle.IsInvalid) return null;
                var buffer = new StringBuilder(260);
                uint size = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
                if (size >= buffer.Capacity) {
                    buffer = new StringBuilder(checked((int)size));
                    size = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
                }
                return size > 0 && size < buffer.Capacity ? Normal(buffer.ToString()) : null;
            }
        }
        catch { return null; }
    }
    public static void Protect(string[] more) {
        var all = new List<string>();
        foreach (var known in new[] {
            Environment.SpecialFolder.UserProfile, Environment.SpecialFolder.MyDocuments,
            Environment.SpecialFolder.DesktopDirectory, Environment.SpecialFolder.ApplicationData,
            Environment.SpecialFolder.LocalApplicationData, Environment.SpecialFolder.ProgramFiles,
            Environment.SpecialFolder.ProgramFilesX86, Environment.SpecialFolder.Windows,
            Environment.SpecialFolder.CommonApplicationData }) {
            try {
                all.Add(Environment.GetFolderPath(known, Environment.SpecialFolderOption.DoNotVerify));
            }
            catch { }
        }
        if (all.Count != 0 && !string.IsNullOrEmpty(all[0])) {
            all.Add(Path.Combine(all[0], "Downloads"));
        }
        all.AddRange(more);
        folders = all.Where(folder => !string.IsNullOrWhiteSpace(folder)).Select(Normal)
            .Where(folder => folder != null).Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
    }
    // The protected folder that *path* is or contains; null when it is neither.
    static string Holds(string full) {
        if (full == null) return null;
        if (string.Equals(Path.GetPathRoot(full), full, StringComparison.OrdinalIgnoreCase)) {
            return full;
        }
        var inside = full + Path.DirectorySeparatorChar;
        foreach (var folder in folders) {
            if (string.Equals(folder, full, StringComparison.OrdinalIgnoreCase) ||
                folder.StartsWith(inside, StringComparison.OrdinalIgnoreCase)) return folder;
        }
        return null;
    }
    static void Stop(PSCmdlet cmdlet, RuntimeException error) {
        // Ends the command like `throw`: try/catch still catches it, -ErrorAction does not.
        try {
            typeof(ErrorRecord).GetMethod("SetInvocationInfo",
                BindingFlags.Instance | BindingFlags.NonPublic)?.Invoke(
                error.ErrorRecord, new object[] { cmdlet.MyInvocation });
        }
        catch { }
        error.WasThrownFromThrowStatement = true;
        throw error;
    }
    static void Refuse(PSCmdlet cmdlet, string message, string id, ErrorCategory category,
        object target) {
        var record = new ErrorRecord(new InvalidOperationException(message), id, category, target);
        Stop(cmdlet, new RuntimeException(message, record.Exception, record));
    }
    static bool Bound(PSCmdlet cmdlet, string name) =>
        cmdlet.MyInvocation.BoundParameters.ContainsKey(name);
    static bool FileSystem(ProviderInfo provider) =>
        provider != null && provider.Name.Equals("FileSystem", StringComparison.OrdinalIgnoreCase);
    // Refuses to remove or move a protected folder, a folder that contains one,
    // or everything directly in one at once (`folder\*`).
    public static void Keep(PSCmdlet cmdlet, string[] paths, bool literal, string verb) {
        if (paths == null || (cmdlet.MyInvocation.BoundParameters.TryGetValue("WhatIf",
            out var whatIf) && LanguagePrimitives.IsTrue(whatIf))) return;
        var selective = Bound(cmdlet, "Include") || Bound(cmdlet, "Filter");
        foreach (var path in paths) {
            if (string.IsNullOrEmpty(path)) continue;
            try {
                ProviderInfo provider;
                PSDriveInfo drive;
                var session = cmdlet.SessionState.Path;
                if (literal || !WildcardPattern.ContainsWildcardCharacters(path)) {
                    var one = session.GetUnresolvedProviderPathFromPSPath(
                        path, out provider, out drive);
                    if (FileSystem(provider)) Check(cmdlet, one, false, path, verb);
                    continue;
                }
                var cut = path.LastIndexOfAny(new[] { '\\', '/' });
                var leaf = path.Substring(cut + 1);
                var parent = cut < 0 ? "." : path.Substring(0, cut + 1);
                if (!selective && leaf.Contains("*") && leaf.Trim('*', '.').Length == 0 &&
                    !WildcardPattern.ContainsWildcardCharacters(parent)) {
                    var directory = session.GetUnresolvedProviderPathFromPSPath(
                        parent, out provider, out drive);
                    if (FileSystem(provider)) Check(cmdlet, directory, true, path, verb);
                }
                foreach (var one in session.GetResolvedProviderPathFromPSPath(path, out provider)) {
                    if (FileSystem(provider)) Check(cmdlet, one, false, path, verb);
                }
            }
            catch (RuntimeException error) when (!error.WasThrownFromThrowStatement) {
                // The cmdlet reports a path it cannot resolve.
            }
        }
    }
    static void Check(PSCmdlet cmdlet, string resolved, bool content, string path, string verb) {
        var full = Normal(resolved);
        var kept = Holds(full);
        if (kept == null) {
            full = DirectoryPath(resolved, content);
            kept = Holds(full);
            if (kept == null) return;
        }
        var message = "Cannot " + verb + (content ? " everything in '" : " '") + full + "'" +
            (content ? " at once" : "") +
            (string.Equals(kept, full, StringComparison.OrdinalIgnoreCase) ? "" :
                ", which contains '" + kept + "'") + ": vBot protects this folder.";
        Refuse(cmdlet, message, "VBotProtectedFolder", ErrorCategory.PermissionDenied, path);
    }
    // An explicitly passed empty path, which the cmdlet would read as another folder.
    public static void Named(PSCmdlet cmdlet, string[] paths, string parameter = "Path") {
        if (!Bound(cmdlet, parameter)) return;
        var missing = paths == null || paths.Length == 0 || paths.Contains(null);
        if (!missing && !paths.Contains("")) return;
        Refuse(cmdlet, "Cannot bind argument to parameter '" + parameter + "' because it is " +
            (missing ? "null." : "an empty string."), "ParameterArgumentValidationError",
            ErrorCategory.InvalidData, null);
    }
    // Set the cmdlet's error action before pipeline input binds, so errors
    // written by that binder or by the provider end the command.
    // PowerShell exposes ErrorAction on its command runtime only internally.
    public static void GuardLocation(PSCmdlet cmdlet) {
        try {
            if (Bound(cmdlet, "ErrorAction")) return;
            var runtime = cmdlet.CommandRuntime;
            var action = runtime?.GetType().GetProperty("ErrorAction",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (action != null && action.CanWrite &&
                Equals(action.GetValue(runtime), ActionPreference.Continue)) {
                action.SetValue(runtime, ActionPreference.Stop);
            }
        }
        catch { } // A runtime without a writable error action keeps its ordinary behavior.
    }
}
[Cmdlet(VerbsCommon.Remove, "Item", DefaultParameterSetName = "Path", SupportsShouldProcess = true,
    SupportsTransactions = true, HelpUri = "https://go.microsoft.com/fwlink/?LinkID=2097103")]
public class VBotRemoveItem : RemoveItemCommand {
    protected override void ProcessRecord() {
        var literal = ParameterSetName == "LiteralPath";
        VBotGuard.Keep(this, literal ? LiteralPath : Path, literal, "remove");
        base.ProcessRecord();
    }
}
[Cmdlet(VerbsCommon.Move, "Item", DefaultParameterSetName = "Path", SupportsShouldProcess = true,
    SupportsTransactions = true, HelpUri = "https://go.microsoft.com/fwlink/?LinkID=2096591")]
public class VBotMoveItem : MoveItemCommand {
    protected override void ProcessRecord() {
        var literal = ParameterSetName == "LiteralPath";
        VBotGuard.Keep(this, literal ? LiteralPath : Path, literal, "move");
        base.ProcessRecord();
    }
}
[Cmdlet(VerbsCommon.Set, "Location", DefaultParameterSetName = "Path", SupportsTransactions = true,
    HelpUri = "https://go.microsoft.com/fwlink/?LinkID=2097049")]
[OutputType(typeof(PathInfo), typeof(PathInfoStack))]
public class VBotSetLocation : SetLocationCommand {
    protected override void BeginProcessing() {
        VBotGuard.GuardLocation(this);
        base.BeginProcessing();
    }
    protected override void ProcessRecord() {
        if (ParameterSetName == "Path") VBotGuard.Named(this, new[] { Path });
        base.ProcessRecord();
    }
}
[Cmdlet(VerbsCommon.Push, "Location", DefaultParameterSetName = "Path", SupportsTransactions = true,
    HelpUri = "https://go.microsoft.com/fwlink/?LinkID=2097105")]
public class VBotPushLocation : PushLocationCommand {
    protected override void BeginProcessing() {
        VBotGuard.GuardLocation(this);
        base.BeginProcessing();
    }
    protected override void ProcessRecord() {
        if (ParameterSetName == "Path") VBotGuard.Named(this, new[] { Path });
        // Unlike Set-Location, Push-Location accepts an empty LiteralPath
        // and pushes the current directory.
        if (ParameterSetName == "LiteralPath") {
            VBotGuard.Named(this, new[] { LiteralPath }, "LiteralPath");
        }
        base.ProcessRecord();
    }
}
[Cmdlet(VerbsCommon.Get, "ChildItem", DefaultParameterSetName = "Items", SupportsTransactions = true,
    HelpUri = "https://go.microsoft.com/fwlink/?LinkID=2096492")]
public class VBotGetChildItem : GetChildItemCommand {
    protected override void ProcessRecord() {
        if (ParameterSetName == "Items") VBotGuard.Named(this, Path);
        base.ProcessRecord();
    }
}
